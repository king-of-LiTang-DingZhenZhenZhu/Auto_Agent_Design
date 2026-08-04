"""Hierarchical SMT physical planning for larger analog blocks.

This module intentionally keeps the SMT problem at the abstraction level that
is useful for early analog layout closure: device clusters, routing corridors,
and critical net resource demand.  Detailed terminal access, exact via choice,
and ECO repair remain downstream local problems.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from math import ceil, sqrt
from typing import Any, Mapping, Sequence

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

from analogskills.blocks import make_brokaw_bandgap_reference, make_pmos_pass_ldo
from analogskills.contracts import TerminalRef, TopologyGraph
from analogskills.env import get_env, has_env, selected_env_name
from analogskills.layout.hierarchical_smt import HierarchicalRouteCandidate, HierarchicalRouteDemand
from analogskills.layout.hierarchical_smt_2d import (
    HierarchicalGroupPlacement2D,
    HierarchicalPhysicalGroup2D,
    HierarchicalPhysicalProblem2D,
    HierarchicalPhysicalSolution2D,
    HierarchicalRoutingCorridor2D,
    solve_hierarchical_physical_problem_2d,
)
from analogskills.layout.placement import Placement
from analogskills.layout.smt_rule_strategy import resolve_smt_rule_strategy
from analogskills.layout.smt_design_rules import nm_to_sites, smt_site_nm
from analogskills.layout.analog_smt_compiler import compile_analog_layout_smt
from analogskills.layout.layout_tweak import apply_layout_tweak_patch_to_spec
from analogskills.layout.macro_refinement import (
    MacroRefinementCandidateSpec,
    aggregate_macro_bboxes_tracks,
    bandgap_free_global_packing_candidates,
    bandgap_reference_min_gap_packing_candidates,
    bandgap_resistor_ladder_refinement_candidates,
    bandgap_split_top_mos_void_insertion_candidates,
    bandgap_top_mos_void_insertion_candidates,
    bandgap_upper_mos_compaction_candidates,
    bandgap_vertical_gap_compaction_candidates,
    baseline_macro_refinement,
    current_passive_realization_guard,
    ldo_human_motif_refinement_candidates,
)


DeviceSizeMap = Mapping[str, tuple[float, float]]


@dataclass(frozen=True)
class AnalogSmtGroupSpec:
    name: str
    members: tuple[str, ...]
    packing: str = "row"
    min_width_tracks: int = 2
    min_height_tracks: int = 2
    allow_rotate: bool = False


@dataclass(frozen=True)
class AnalogHierarchicalSmtResult:
    block: str
    graph: TopologyGraph
    problem: HierarchicalPhysicalProblem2D
    physical: HierarchicalPhysicalSolution2D
    group_specs: Mapping[str, AnalogSmtGroupSpec]
    track_pitch_um: float
    checks: Mapping[str, object] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks.get("passed", False))


@dataclass(frozen=True)
class AnalogStructuredRouteResult:
    plan: object
    summary: Mapping[str, object]


@dataclass(frozen=True)
class AnalogFlatCompactSmtResult:
    """Device-level compact SMT refinement for analog blocks.

    The hierarchical solver remains useful for route-capacity accounting.  This
    result refines final PCell instance placement at device granularity and
    recomputes corridor boxes from that compact placement, so structured routing
    uses the same final geometry.
    """

    block: str
    graph: TopologyGraph
    base: AnalogHierarchicalSmtResult
    placements: tuple[Placement, ...]
    group_bboxes_tracks: Mapping[str, tuple[int, int, int, int]]
    corridor_bboxes_um: Mapping[str, tuple[float, float, float, float]]
    track_pitch_um: float
    checks: Mapping[str, object] = field(default_factory=dict)
    routing_origin: str = "flat_compact_smt_structured"

    @property
    def passed(self) -> bool:
        return bool(self.checks.get("passed", False))


@dataclass(frozen=True)
class _StructuredTerminalAnchor:
    xy_um: tuple[float, float]
    layer: str
    contact_layer: str = ""
    instance: str = ""
    logical_name: str = ""
    terminal: str = ""


@dataclass(frozen=True)
class _StructuredTransitionEscape:
    dx_um: float
    dy_um: float
    width_um: float
    merge_landing: bool = False
    merge_width_um: float = 0.0
    merge_style: str = "segment"


DEFAULT_LDO_CRITICAL_TRACK_DEMAND = {
    "REFERENCE_FEEDBACK_SENSE": 2,
    "LOAD_GATE_CONTROL": 2,
    "PASS_GATE_CONTROL": 2,
    "OUTPUT_CURRENT": 4,
    "INPUT_SUPPLY_CURRENT": 4,
    "GROUND_RETURN": 3,
}

DEFAULT_LDO_NONCRITICAL_TRACK_DEMAND = {
    "TAIL_BIAS": 1,
}

DEFAULT_BANDGAP_CRITICAL_TRACK_DEMAND = {
    "DELTA_VBE_SENSE": 2,
    "EA_OUTPUT": 2,
    "REFERENCE_OUTPUT": 3,
    "SUPPLY_RETURN": 4,
}

DEFAULT_BANDGAP_NONCRITICAL_TRACK_DEMAND = {
    "BIAS_N": 1,
}


def build_ldo_hierarchical_problem(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    track_pitch_um: float = 0.5,
) -> HierarchicalPhysicalProblem2D:
    graph = graph or make_pmos_pass_ldo("SMT_LDO")
    names = _ldo_device_names(graph)
    group_specs = _ldo_group_specs(names)
    rules = _analog_block_rule_config(
        pdk,
        "ldo",
        (
            "C_TAIL_INPUT",
            "C_INPUT_LOAD",
            "C_LOAD_PASS",
            "C_PASS_FEEDBACK",
            "C_INPUT_FEEDBACK",
            "C_PASS_OUTPUT_CAP",
        ),
        DEFAULT_LDO_CRITICAL_TRACK_DEMAND,
        DEFAULT_LDO_NONCRITICAL_TRACK_DEMAND,
        default_target_aspect=(3, 2),
    )
    pair_spacing_um_by_role = _pair_spacing_um_by_role(rules)
    groups = tuple(
        _physical_group_from_spec(
            spec,
            device_sizes_um or {},
            track_pitch_um,
            spacing_um=pair_spacing_um_by_role.get(spec.name),
        )
        for spec in group_specs
    )

    corridor_rules = dict(rules["corridors"])

    def ckwargs(name: str) -> dict[str, object]:
        return dict(corridor_rules.get(name, {}))

    corridors = (
        HierarchicalRoutingCorridor2D("C_TAIL_INPUT", "tail_source", "input_pair", "vertical", **ckwargs("C_TAIL_INPUT")),
        HierarchicalRoutingCorridor2D("C_INPUT_LOAD", "input_pair", "load_pair", "vertical", **ckwargs("C_INPUT_LOAD")),
        HierarchicalRoutingCorridor2D("C_LOAD_PASS", "load_pair", "pass_device", "horizontal", **ckwargs("C_LOAD_PASS")),
        HierarchicalRoutingCorridor2D("C_PASS_FEEDBACK", "pass_device", "feedback_output", "horizontal", **ckwargs("C_PASS_FEEDBACK")),
        HierarchicalRoutingCorridor2D("C_INPUT_FEEDBACK", "input_pair", "feedback_output", "horizontal", **ckwargs("C_INPUT_FEEDBACK")),
        HierarchicalRoutingCorridor2D("C_PASS_OUTPUT_CAP", "pass_device", "output_cap_bank", "horizontal", **ckwargs("C_PASS_OUTPUT_CAP")),
    )
    critical_cfg = dict(rules["critical_track_demand"])
    noncritical_cfg = dict(rules["noncritical_track_demand"])
    critical = (
        HierarchicalRouteDemand(
            "REFERENCE_FEEDBACK_SENSE",
            int(critical_cfg.get("REFERENCE_FEEDBACK_SENSE", 2)),
            (HierarchicalRouteCandidate("SENSE_TO_INPUT", ("C_INPUT_FEEDBACK",), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "PASS_GATE_CONTROL",
            int(critical_cfg.get("PASS_GATE_CONTROL", 2)),
            (HierarchicalRouteCandidate("EA_TO_PASS", ("C_INPUT_LOAD", "C_LOAD_PASS"), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "OUTPUT_CURRENT",
            int(critical_cfg.get("OUTPUT_CURRENT", 4)),
            (HierarchicalRouteCandidate("PASS_TO_OUTPUT", ("C_PASS_OUTPUT_CAP",), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "INPUT_SUPPLY_CURRENT",
            int(critical_cfg.get("INPUT_SUPPLY_CURRENT", 4)),
            (HierarchicalRouteCandidate("VIN_TO_PASS", ("C_LOAD_PASS",), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "GROUND_RETURN",
            int(critical_cfg.get("GROUND_RETURN", 3)),
            (HierarchicalRouteCandidate("VSS_BACKBONE", ("C_TAIL_INPUT", "C_PASS_OUTPUT_CAP"), cost=2),),
            critical=True,
        ),
    )
    noncritical = (
        HierarchicalRouteDemand(
            "TAIL_BIAS",
            int(noncritical_cfg.get("TAIL_BIAS", 1)),
            (HierarchicalRouteCandidate("BIAS_TO_TAIL", ("C_TAIL_INPUT",), cost=1),),
        ),
    )
    return HierarchicalPhysicalProblem2D(
        groups=groups,
        corridors=corridors,
        critical_routes=critical,
        noncritical_routes=noncritical,
        placement_spacing_tracks=int(rules["placement_spacing_tracks"]),
        target_aspect_num=int(rules["target_aspect_num"]),
        target_aspect_den=int(rules["target_aspect_den"]),
        rule_metadata={**rules, "block": "ldo", "group_specs": {spec.name: spec.members for spec in group_specs}},
    )


def run_ldo_hierarchical_flow(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    track_pitch_um: float = 0.5,
) -> AnalogHierarchicalSmtResult:
    graph = graph or make_pmos_pass_ldo("SMT_LDO")
    specs = {spec.name: spec for spec in _ldo_group_specs(_ldo_device_names(graph))}
    problem = build_ldo_hierarchical_problem(graph, pdk=pdk, device_sizes_um=device_sizes_um, track_pitch_um=track_pitch_um)
    physical = solve_hierarchical_physical_problem_2d(problem)
    checks = _analog_smt_checks("ldo", graph, problem, physical, specs)
    return AnalogHierarchicalSmtResult("ldo", graph, problem, physical, specs, track_pitch_um, checks)


def run_ldo_flat_compact_flow(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    sizing: Mapping[str, Mapping[str, object]] | None = None,
    track_pitch_um: float = 0.5,
    base_result: AnalogHierarchicalSmtResult | None = None,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
    layout_tweak_patch: Mapping[str, Any] | None = None,
    solver_timeout_ms: int | None = None,
    max_candidate_count: int | None = None,
    max_refinement_candidates: int | None = None,
) -> AnalogFlatCompactSmtResult:
    graph = graph or make_pmos_pass_ldo("SMT_LDO")
    base = base_result or run_ldo_hierarchical_flow(graph, pdk=pdk, device_sizes_um=device_sizes_um, track_pitch_um=track_pitch_um)
    names = _ldo_device_names(graph)
    specs = {spec.name: spec for spec in _ldo_group_specs(names)}
    return _run_flat_compact_smt(
        "ldo",
        graph,
        base,
        specs,
        pdk=pdk,
        device_sizes_um=device_sizes_um or {},
        sizing=sizing,
        track_pitch_um=track_pitch_um,
        calibration_cache=calibration_cache,
        pcell_calibre_catalog=pcell_calibre_catalog,
        layout_tweak_patch=layout_tweak_patch,
        solver_timeout_ms=solver_timeout_ms,
        max_candidate_count=max_candidate_count,
        max_refinement_candidates=max_refinement_candidates,
    )


def lower_ldo_smt_device_placements(
    result_or_physical: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult | HierarchicalPhysicalSolution2D,
    *,
    graph: TopologyGraph | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    track_pitch_um: float | None = None,
) -> tuple[Placement, ...]:
    if isinstance(result_or_physical, AnalogFlatCompactSmtResult):
        return tuple(result_or_physical.placements)
    if isinstance(result_or_physical, AnalogHierarchicalSmtResult):
        physical = result_or_physical.physical
        graph = graph or result_or_physical.graph
        pitch = result_or_physical.track_pitch_um if track_pitch_um is None else track_pitch_um
        pair_spacing_um_by_role = _pair_spacing_um_by_role(result_or_physical.problem.rule_metadata)
    else:
        physical = result_or_physical
        graph = graph or make_pmos_pass_ldo("SMT_LDO")
        pitch = 0.5 if track_pitch_um is None else track_pitch_um
        pair_spacing_um_by_role = {}
    names = _ldo_device_names(graph)
    sizes = device_sizes_um or {}
    placements: list[Placement] = []
    placements.extend(_place_row(physical.master.placements["tail_source"], (names["tail"],), sizes, pitch, role="tail_source"))
    placements.extend(
        _place_symmetric_pair(
            physical.master.placements["input_pair"],
            names["input_pair"],
            sizes,
            pitch,
            role="input_pair",
            spacing_um=pair_spacing_um_by_role.get("input_pair"),
        )
    )
    placements.extend(
        _place_symmetric_pair(
            physical.master.placements["load_pair"],
            names["load_pair"],
            sizes,
            pitch,
            role="load_pair",
            spacing_um=pair_spacing_um_by_role.get("load_pair"),
        )
    )
    placements.extend(_place_row(physical.master.placements["pass_device"], (names["pass"],), sizes, pitch, role="pass_device"))
    placements.extend(_place_row(physical.master.placements["feedback_output"], names["feedback"], sizes, pitch, role="feedback_output"))
    placements.extend(_place_row(physical.master.placements["output_cap_bank"], names["output_cap"], sizes, pitch, role="output_cap_bank"))
    return tuple(placements)


def build_ldo_smt_structured_interconnect_plan(
    graph: TopologyGraph,
    pcell_plan: object,
    pdk: object,
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
    *,
    lib: str,
    cell: str,
    calibration_cache: object | None = None,
) -> AnalogStructuredRouteResult:
    """Build LDO critical routes from the hierarchical SMT solution only."""

    bias_net = _existing_net_or_pin_net(graph, "BIAS", "BIAS_N")
    tail_net = _existing_net_or_pin_net(graph, "STAIL", "TAIL")
    route_specs = (
        ("VIN", ("C_LOAD_PASS",), _configured_route_layer(pdk, "ldo", "VIN", 1), 0.4, _configured_route_lane(pdk, "ldo", "VIN", 0), "INPUT_SUPPLY_CURRENT"),
        ("VOUT", ("C_LOAD_PASS", "C_PASS_FEEDBACK"), _configured_route_layer(pdk, "ldo", "VOUT", 2), 0.4, _configured_route_lane(pdk, "ldo", "VOUT", 1), "OUTPUT_CURRENT"),
        ("VSS", ("C_TAIL_INPUT", "C_PASS_FEEDBACK"), _configured_route_layer(pdk, "ldo", "VSS", 3), 0.36, _configured_route_lane(pdk, "ldo", "VSS", -1), "GROUND_RETURN"),
        ("VREF", ("C_INPUT_LOAD",), _configured_route_layer(pdk, "ldo", "VREF", 0), 0.16, _configured_route_lane(pdk, "ldo", "VREF", -1), "REFERENCE_FEEDBACK_SENSE"),
        ("VFB", ("C_INPUT_LOAD", "C_PASS_FEEDBACK"), _configured_route_layer(pdk, "ldo", "VFB", 4), 0.16, _configured_route_lane(pdk, "ldo", "VFB", 1), "REFERENCE_FEEDBACK_SENSE"),
        ("VGATE_LOAD", ("C_INPUT_LOAD",), _configured_route_layer(pdk, "ldo", "VGATE_LOAD", 4), 0.18, _configured_route_lane(pdk, "ldo", "VGATE_LOAD", -2), "LOAD_GATE_CONTROL"),
        ("VGATE_PASS", ("C_INPUT_LOAD", "C_LOAD_PASS"), _configured_route_layer(pdk, "ldo", "VGATE_PASS", 5), 0.18, _configured_route_lane(pdk, "ldo", "VGATE_PASS", 0), "PASS_GATE_CONTROL"),
        (bias_net, ("C_TAIL_INPUT",), _configured_route_layer(pdk, "ldo", bias_net, 6), 0.14, _configured_route_lane(pdk, "ldo", bias_net, 0), "TAIL_BIAS"),
        (tail_net, ("C_TAIL_INPUT",), _configured_route_layer(pdk, "ldo", tail_net, 7), 0.18, _configured_route_lane(pdk, "ldo", tail_net, 1), "TAIL_BIAS"),
    )
    return _build_structured_interconnect_plan(
        graph,
        pcell_plan,
        pdk,
        smt_result,
        route_specs,
        lib=lib,
        cell=cell,
        calibration_cache=calibration_cache,
    )


def build_bandgap_hierarchical_problem(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    track_pitch_um: float = 0.5,
) -> HierarchicalPhysicalProblem2D:
    graph = graph or make_brokaw_bandgap_reference("SMT_BGR")
    names = _bandgap_device_names(graph)
    group_specs = _bandgap_group_specs(names)
    rules = _analog_block_rule_config(
        pdk,
        "bandgap",
        ("C_TAIL_INPUT", "C_INPUT_LOAD", "C_BJT_MIRROR", "C_BJT_RES", "C_BJT_INPUT", "C_MIRROR_LOAD"),
        DEFAULT_BANDGAP_CRITICAL_TRACK_DEMAND,
        DEFAULT_BANDGAP_NONCRITICAL_TRACK_DEMAND,
        default_target_aspect=(4, 1),
    )
    pair_spacing_um_by_role = _pair_spacing_um_by_role(rules)
    groups = tuple(
        _physical_group_from_spec(
            spec,
            device_sizes_um or {},
            track_pitch_um,
            spacing_um=pair_spacing_um_by_role.get(spec.name),
        )
        for spec in group_specs
    )
    corridor_rules = dict(rules["corridors"])

    def ckwargs(name: str) -> dict[str, object]:
        return dict(corridor_rules.get(name, {}))

    corridors = (
        HierarchicalRoutingCorridor2D("C_TAIL_INPUT", "tail_source", "input_pair", "vertical", **ckwargs("C_TAIL_INPUT")),
        HierarchicalRoutingCorridor2D("C_INPUT_LOAD", "input_pair", "load_pair", "vertical", **ckwargs("C_INPUT_LOAD")),
        HierarchicalRoutingCorridor2D("C_BJT_MIRROR", "bjt_core", "pmos_mirror", "vertical", **ckwargs("C_BJT_MIRROR")),
        HierarchicalRoutingCorridor2D("C_BJT_RES", "bjt_core", "resistor_ladder", "horizontal", **ckwargs("C_BJT_RES")),
        HierarchicalRoutingCorridor2D("C_BJT_INPUT", "bjt_core", "input_pair", "horizontal", **ckwargs("C_BJT_INPUT")),
        HierarchicalRoutingCorridor2D("C_MIRROR_LOAD", "pmos_mirror", "load_pair", "horizontal", **ckwargs("C_MIRROR_LOAD")),
    )
    critical_cfg = dict(rules["critical_track_demand"])
    noncritical_cfg = dict(rules["noncritical_track_demand"])
    critical = (
        HierarchicalRouteDemand(
            "DELTA_VBE_SENSE",
            int(critical_cfg.get("DELTA_VBE_SENSE", 2)),
            (HierarchicalRouteCandidate("BJT_TO_INPUT_MATCHED", ("C_BJT_INPUT",), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "EA_OUTPUT",
            int(critical_cfg.get("EA_OUTPUT", 2)),
            (HierarchicalRouteCandidate("EA_TO_PMOS_GATES", ("C_INPUT_LOAD", "C_MIRROR_LOAD"), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "REFERENCE_OUTPUT",
            int(critical_cfg.get("REFERENCE_OUTPUT", 3)),
            (HierarchicalRouteCandidate("BJT_RES_REF", ("C_BJT_RES", "C_BJT_INPUT"), cost=1),),
            critical=True,
        ),
        HierarchicalRouteDemand(
            "SUPPLY_RETURN",
            int(critical_cfg.get("SUPPLY_RETURN", 4)),
            (HierarchicalRouteCandidate("VDD_VSS_BACKBONE", ("C_BJT_MIRROR", "C_INPUT_LOAD"), cost=2),),
            critical=True,
        ),
    )
    noncritical = (
        HierarchicalRouteDemand(
            "BIAS_N",
            int(noncritical_cfg.get("BIAS_N", 1)),
            (HierarchicalRouteCandidate("BIAS_TO_TAIL", ("C_TAIL_INPUT",), cost=1),),
        ),
    )
    return HierarchicalPhysicalProblem2D(
        groups=groups,
        corridors=corridors,
        critical_routes=critical,
        noncritical_routes=noncritical,
        placement_spacing_tracks=int(rules["placement_spacing_tracks"]),
        target_aspect_num=int(rules["target_aspect_num"]),
        target_aspect_den=int(rules["target_aspect_den"]),
        rule_metadata={**rules, "block": "bandgap", "group_specs": {spec.name: spec.members for spec in group_specs}},
    )


def run_bandgap_hierarchical_flow(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    track_pitch_um: float = 0.5,
) -> AnalogHierarchicalSmtResult:
    graph = graph or make_brokaw_bandgap_reference("SMT_BGR")
    specs = {spec.name: spec for spec in _bandgap_group_specs(_bandgap_device_names(graph))}
    problem = build_bandgap_hierarchical_problem(graph, pdk=pdk, device_sizes_um=device_sizes_um, track_pitch_um=track_pitch_um)
    physical = solve_hierarchical_physical_problem_2d(problem)
    checks = _analog_smt_checks("bandgap", graph, problem, physical, specs)
    return AnalogHierarchicalSmtResult("bandgap", graph, problem, physical, specs, track_pitch_um, checks)


def run_bandgap_flat_compact_flow(
    graph: TopologyGraph | None = None,
    *,
    pdk: object | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    sizing: Mapping[str, Mapping[str, object]] | None = None,
    track_pitch_um: float = 0.5,
    base_result: AnalogHierarchicalSmtResult | None = None,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
    layout_tweak_patch: Mapping[str, Any] | None = None,
    solver_timeout_ms: int | None = None,
    max_candidate_count: int | None = None,
    max_refinement_candidates: int | None = None,
) -> AnalogFlatCompactSmtResult:
    graph = graph or make_brokaw_bandgap_reference("SMT_BGR")
    base = base_result or run_bandgap_hierarchical_flow(graph, pdk=pdk, device_sizes_um=device_sizes_um, track_pitch_um=track_pitch_um)
    names = _bandgap_device_names(graph)
    specs = {spec.name: spec for spec in _bandgap_group_specs(names)}
    return _run_flat_compact_smt(
        "bandgap",
        graph,
        base,
        specs,
        pdk=pdk,
        device_sizes_um=device_sizes_um or {},
        sizing=sizing,
        track_pitch_um=track_pitch_um,
        calibration_cache=calibration_cache,
        pcell_calibre_catalog=pcell_calibre_catalog,
        layout_tweak_patch=layout_tweak_patch,
        solver_timeout_ms=solver_timeout_ms,
        max_candidate_count=max_candidate_count,
        max_refinement_candidates=max_refinement_candidates,
    )


def lower_bandgap_smt_device_placements(
    result_or_physical: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult | HierarchicalPhysicalSolution2D,
    *,
    graph: TopologyGraph | None = None,
    device_sizes_um: DeviceSizeMap | None = None,
    track_pitch_um: float | None = None,
) -> tuple[Placement, ...]:
    if isinstance(result_or_physical, AnalogFlatCompactSmtResult):
        return tuple(result_or_physical.placements)
    if isinstance(result_or_physical, AnalogHierarchicalSmtResult):
        physical = result_or_physical.physical
        graph = graph or result_or_physical.graph
        pitch = result_or_physical.track_pitch_um if track_pitch_um is None else track_pitch_um
        pair_spacing_um_by_role = _pair_spacing_um_by_role(result_or_physical.problem.rule_metadata)
    else:
        physical = result_or_physical
        graph = graph or make_brokaw_bandgap_reference("SMT_BGR")
        pitch = 0.5 if track_pitch_um is None else track_pitch_um
        pair_spacing_um_by_role = {}
    names = _bandgap_device_names(graph)
    sizes = device_sizes_um or {}
    groups = physical.master.placements
    placements: list[Placement] = []
    placements.extend(_place_grid(groups["bjt_core"], names["bjt_core"], sizes, pitch, role="bjt_core", prefer_center=names.get("q1", "")))
    placements.extend(_place_row(groups["resistor_ladder"], names["resistor_ladder"], sizes, pitch, role="resistor_ladder"))
    placements.extend(
        _place_symmetric_pair(
            groups["pmos_mirror"],
            names["pmos_mirror"],
            sizes,
            pitch,
            role="pmos_mirror",
            spacing_um=pair_spacing_um_by_role.get("pmos_mirror"),
        )
    )
    placements.extend(
        _place_symmetric_pair(
            groups["input_pair"],
            names["input_pair"],
            sizes,
            pitch,
            role="input_pair",
            spacing_um=pair_spacing_um_by_role.get("input_pair"),
        )
    )
    placements.extend(
        _place_symmetric_pair(
            groups["load_pair"],
            names["load_pair"],
            sizes,
            pitch,
            role="load_pair",
            spacing_um=pair_spacing_um_by_role.get("load_pair"),
        )
    )
    placements.extend(_place_row(groups["tail_source"], (names["tail"],), sizes, pitch, role="tail_source"))
    return tuple(placements)


def build_bandgap_smt_structured_interconnect_plan(
    graph: TopologyGraph,
    pcell_plan: object,
    pdk: object,
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
    *,
    lib: str,
    cell: str,
    calibration_cache: object | None = None,
    fixed_obstacle_plan: object | None = None,
) -> AnalogStructuredRouteResult:
    """Build bandgap critical routes from the hierarchical SMT solution only."""

    vref_net = _existing_net_or_pin_net(graph, "VREF")
    bias_net = _existing_net_or_pin_net(graph, "BIAS_N", "BIAS")
    diode1_net = _existing_net_or_pin_net(graph, "diode1")
    route_specs: list[tuple[str, Sequence[str], str, float, int, str]] = [
        ("VDD", ("C_BJT_MIRROR", "C_INPUT_LOAD", "C_MIRROR_LOAD"), _configured_route_layer(pdk, "bandgap", "VDD", 1), 0.36, _configured_route_lane(pdk, "bandgap", "VDD", -1), "SUPPLY_RETURN"),
        ("VSS", ("C_TAIL_INPUT", "C_BJT_RES", "C_INPUT_LOAD"), _configured_route_layer(pdk, "bandgap", "VSS", 2), 0.36, _configured_route_lane(pdk, "bandgap", "VSS", 1), "SUPPLY_RETURN"),
        ("diode2", ("C_BJT_INPUT",), _configured_route_layer(pdk, "bandgap", "diode2", 3), 0.16, _configured_route_lane(pdk, "bandgap", "diode2", 1), "DELTA_VBE_SENSE"),
        (vref_net, ("C_BJT_RES", "C_BJT_INPUT"), _configured_route_layer(pdk, "bandgap", vref_net, 4), 0.22, _configured_route_lane(pdk, "bandgap", vref_net, 0), "REFERENCE_OUTPUT"),
        ("amp_left", ("C_INPUT_LOAD",), _configured_route_layer(pdk, "bandgap", "amp_left", 4), 0.10, _configured_route_lane(pdk, "bandgap", "amp_left", -1), "EA_LOCAL_MIRROR"),
        ("ea_out", ("C_INPUT_LOAD", "C_MIRROR_LOAD"), _configured_route_layer(pdk, "bandgap", "ea_out", 5), 0.18, _configured_route_lane(pdk, "bandgap", "ea_out", 0), "EA_OUTPUT"),
        ("TAIL", ("C_TAIL_INPUT",), _configured_route_layer(pdk, "bandgap", "TAIL", 1), 0.18, _configured_route_lane(pdk, "bandgap", "TAIL", 1), "BIAS_N"),
        (bias_net, ("C_TAIL_INPUT",), _configured_route_layer(pdk, "bandgap", bias_net, 0), 0.14, _configured_route_lane(pdk, "bandgap", bias_net, -1), "BIAS_N"),
        ("nR1", ("C_BJT_RES",), _configured_route_layer(pdk, "bandgap", "nR1", 5), 0.16, _configured_route_lane(pdk, "bandgap", "nR1", 1), "REFERENCE_OUTPUT"),
        ("nR2", ("C_BJT_RES",), _configured_route_layer(pdk, "bandgap", "nR2", 6), 0.16, _configured_route_lane(pdk, "bandgap", "nR2", -1), "REFERENCE_OUTPUT"),
    ]
    if diode1_net in graph.nets and diode1_net != vref_net:
        route_specs.insert(2, (diode1_net, ("C_BJT_INPUT",), _configured_route_layer(pdk, "bandgap", diode1_net, 0), 0.16, _configured_route_lane(pdk, "bandgap", diode1_net, -1), "DELTA_VBE_SENSE"))
    for mid_net in _bandgap_resistor_ladder_mid_nets(graph):
        route_specs.append(
            (
                mid_net,
                (),
                _configured_route_layer(pdk, "bandgap", mid_net, 0),
                0.08,
                _configured_route_lane(pdk, "bandgap", mid_net, 0),
                "REFERENCE_RESISTOR_LADDER",
            )
        )
    return _build_structured_interconnect_plan(
        graph,
        pcell_plan,
        pdk,
        smt_result,
        tuple(route_specs),
        lib=lib,
        cell=cell,
        calibration_cache=calibration_cache,
        fixed_obstacle_plan=fixed_obstacle_plan,
    )


def pcell_instance_sizes_um(pcell_plan: object) -> dict[str, tuple[float, float]]:
    """Extract device bbox estimates from a generated PCell plan."""
    result: dict[str, tuple[float, float]] = {}
    for inst in tuple(getattr(pcell_plan, "instances", ()) or ()):
        name = str(getattr(inst, "name", "") or "")
        if not name:
            continue
        try:
            width = max(float(getattr(inst, "width_um")), 0.1)
            height = max(float(getattr(inst, "height_um")), 0.1)
        except (TypeError, ValueError):
            continue
        result[name] = (width, height)
    return result


def apply_selected_pcell_realizations_to_sizing(
    sizing: Mapping[str, Mapping[str, object]],
    checks_or_result: Mapping[str, object] | AnalogFlatCompactSmtResult,
) -> dict[str, dict[str, object]]:
    """Return sizing updated with PCell realizations selected by flat SMT.

    Placement uses the selected realization bbox.  This helper must be applied
    before the final ``generate_pcell_layout_plan`` call so the real PCell
    instances are generated with the same nf/m/passive bbox selected by SMT.
    """

    checks = checks_or_result.checks if isinstance(checks_or_result, AnalogFlatCompactSmtResult) else checks_or_result
    selected = _mapping(checks.get("selected_pcell_realizations", {}))
    updated: dict[str, dict[str, object]] = {str(name): dict(row) for name, row in dict(sizing).items()}
    for device, row_obj in selected.items():
        row = _mapping(row_obj)
        if not row:
            continue
        target = updated.setdefault(str(device), {})
        target.update(dict(_mapping(row.get("sizing_overrides", {}))))
        pcell_overrides = dict(_mapping(target.get("pcell_overrides", {})))
        pcell_overrides.update(dict(_mapping(row.get("pcell_overrides", {}))))
        if pcell_overrides:
            target["pcell_overrides"] = pcell_overrides
        try:
            width_um = float(row.get("width_um", 0.0) or 0.0)
            height_um = float(row.get("height_um", 0.0) or 0.0)
        except (TypeError, ValueError):
            width_um, height_um = 0.0, 0.0
        if width_um > 0.0 and height_um > 0.0:
            target["layout_width_um"] = width_um
            target["layout_height_um"] = height_um
    return updated


def _macro_refinement_candidates_for_flat_smt(
    block: str,
    spec: object,
    graph: TopologyGraph,
    pdk: object | None,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    baseline = baseline_macro_refinement(spec)  # type: ignore[arg-type]
    candidates: list[MacroRefinementCandidateSpec] = []
    passive_guard = current_passive_realization_guard(spec)  # type: ignore[arg-type]
    if passive_guard is not None:
        candidates.append(passive_guard)
    candidates.append(baseline)
    if not _macro_refinement_enabled(block, pdk):
        return _limit_macro_refinement_candidates(tuple(candidates), block, pdk)
    if block == "ldo":
        candidates.extend(ldo_human_motif_refinement_candidates(spec, graph))
    if block == "bandgap":
        candidates.extend(
            bandgap_free_global_packing_candidates(
                spec,  # type: ignore[arg-type]
                graph,
            )
        )
        candidates.extend(
            bandgap_reference_min_gap_packing_candidates(
                spec,  # type: ignore[arg-type]
                graph,
            )
        )
        candidates.extend(
            bandgap_top_mos_void_insertion_candidates(
                spec,  # type: ignore[arg-type]
                graph,
            )
        )
        candidates.extend(
            bandgap_split_top_mos_void_insertion_candidates(
                spec,  # type: ignore[arg-type]
                graph,
            )
        )
        candidates.extend(
            bandgap_vertical_gap_compaction_candidates(
                spec,  # type: ignore[arg-type]
                graph,
            )
        )
        candidates.extend(
            bandgap_upper_mos_compaction_candidates(
                spec,  # type: ignore[arg-type]
                graph,
            )
        )
        candidates.extend(
            bandgap_resistor_ladder_refinement_candidates(
                spec,  # type: ignore[arg-type]
                graph,
                max_candidates=_macro_refinement_max_candidates(block, pdk),
            )
        )
    return _limit_macro_refinement_candidates(tuple(candidates), block, pdk)


def _macro_refinement_enabled(block: str, pdk: object | None) -> bool:
    env_name = f"{str(block).upper()}_MACRO_REFINEMENT"
    if env_name in os.environ:
        return _truthy_env(os.environ.get(env_name))
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    layout_cfg = metadata.get("layout", {})
    layout_cfg = layout_cfg if isinstance(layout_cfg, Mapping) else {}
    compact_cfg = layout_cfg.get("macro_refinement", {})
    compact_cfg = compact_cfg if isinstance(compact_cfg, Mapping) else {}
    block_cfg = compact_cfg.get(str(block), compact_cfg)
    block_cfg = block_cfg if isinstance(block_cfg, Mapping) else {}
    return bool(block_cfg.get("enabled", False))


def _truthy_env(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "none"}
    return bool(value)


def configure_analog_global_compaction_env(
    block: str,
    *,
    default_true_area_weight: int = 10,
    default_flat_smt_timeout_ms: int | None = None,
    default_flat_smt_max_candidates: int | None = None,
    default_macro_refinement: bool | None = None,
    default_macro_total_candidates: int | None = None,
    default_macro_candidates: int | None = None,
    default_flat_refinement_aspect_weight: int | None = None,
) -> dict[str, object]:
    """Configure the shared global placement optimization knobs.

    This is intentionally environment-backed because the existing GDS
    generation scripts already call the shared flat SMT flow.  The helper sets
    only missing values, so command-line/session overrides remain authoritative.
    PCell realization SMT is not disabled here.
    """

    block_name = str(block).strip().lower()
    block_upper = block_name.upper()
    global_enable_name = selected_env_name("ANALOG_GLOBAL_COMPACTION")
    block_enable_name = f"{block_upper}_GLOBAL_COMPACTION"
    enabled = True
    if global_enable_name in os.environ:
        enabled = _truthy_env(os.environ.get(global_enable_name))
    if block_enable_name in os.environ:
        enabled = _truthy_env(os.environ.get(block_enable_name))

    applied: dict[str, object] = {
        "block": block_name,
        "enabled": bool(enabled),
        "true_area_weight": None,
        "flat_smt_timeout_ms": None,
        "flat_smt_max_candidates": None,
        "macro_refinement": None,
        "macro_refinement_max_total_candidates": None,
        "macro_refinement_max_candidates": None,
        "flat_refinement_aspect_weight": None,
        "env": {},
        "overrides_respected": {},
    }
    env_report = applied["env"]
    overrides = applied["overrides_respected"]
    if not isinstance(env_report, dict) or not isinstance(overrides, dict):
        return applied
    if not enabled:
        return applied

    true_area_env = f"{block_upper}_FLAT_SMT_TRUE_AREA_WEIGHT"
    true_area_global_env = selected_env_name("FLAT_SMT_TRUE_AREA_WEIGHT")
    if true_area_env in os.environ:
        overrides[true_area_env] = os.environ.get(true_area_env)
    else:
        value = os.environ.get(true_area_global_env, str(max(1, int(default_true_area_weight))))
        os.environ[true_area_env] = value
        env_report[true_area_env] = value
    applied["true_area_weight"] = _flat_smt_env_positive_int(block_name, "TRUE_AREA_WEIGHT")

    if default_flat_smt_timeout_ms is not None:
        timeout_env = f"{block_upper}_FLAT_SMT_TIMEOUT_MS"
        if timeout_env in os.environ:
            overrides[timeout_env] = os.environ.get(timeout_env)
        else:
            os.environ[timeout_env] = str(max(1, int(default_flat_smt_timeout_ms)))
            env_report[timeout_env] = os.environ[timeout_env]
        applied["flat_smt_timeout_ms"] = _flat_smt_env_positive_int(block_name, "TIMEOUT_MS")

    if default_flat_smt_max_candidates is not None:
        max_candidate_env = f"{block_upper}_FLAT_SMT_MAX_CANDIDATES"
        if max_candidate_env in os.environ:
            overrides[max_candidate_env] = os.environ.get(max_candidate_env)
        else:
            os.environ[max_candidate_env] = str(max(1, int(default_flat_smt_max_candidates)))
            env_report[max_candidate_env] = os.environ[max_candidate_env]
        applied["flat_smt_max_candidates"] = _flat_smt_env_positive_int(block_name, "MAX_CANDIDATES")

    if default_macro_refinement is not None:
        macro_env = f"{block_upper}_MACRO_REFINEMENT"
        if macro_env in os.environ:
            overrides[macro_env] = os.environ.get(macro_env)
        else:
            os.environ[macro_env] = "1" if bool(default_macro_refinement) else "0"
            env_report[macro_env] = os.environ[macro_env]
        applied["macro_refinement"] = _truthy_env(os.environ.get(macro_env))

    if default_macro_total_candidates is not None:
        total_env = f"{block_upper}_MACRO_REFINEMENT_MAX_TOTAL_CANDIDATES"
        if total_env in os.environ:
            overrides[total_env] = os.environ.get(total_env)
        else:
            os.environ[total_env] = str(max(1, int(default_macro_total_candidates)))
            env_report[total_env] = os.environ[total_env]
        applied["macro_refinement_max_total_candidates"] = _macro_refinement_max_total_candidates(block_name, None)

    if default_macro_candidates is not None:
        count_env = f"{block_upper}_MACRO_REFINEMENT_MAX_CANDIDATES"
        if count_env in os.environ:
            overrides[count_env] = os.environ.get(count_env)
        else:
            os.environ[count_env] = str(max(1, int(default_macro_candidates)))
            env_report[count_env] = os.environ[count_env]
        applied["macro_refinement_max_candidates"] = _macro_refinement_max_candidates(block_name, None)

    if default_flat_refinement_aspect_weight is not None:
        aspect_env = f"{block_upper}_FLAT_REFINEMENT_ASPECT_WEIGHT"
        if aspect_env in os.environ:
            overrides[aspect_env] = os.environ.get(aspect_env)
        else:
            os.environ[aspect_env] = str(max(0, int(default_flat_refinement_aspect_weight)))
            env_report[aspect_env] = os.environ[aspect_env]
        applied["flat_refinement_aspect_weight"] = _flat_refinement_aspect_weight(block_name)

    applied["pcell_realization_smt_disabled"] = _flat_smt_env_enabled(block_name, "DISABLE_PCELL_REALIZATIONS")
    return applied


def _macro_refinement_max_candidates(block: str, pdk: object | None) -> int:
    env_name = f"{str(block).upper()}_MACRO_REFINEMENT_MAX_CANDIDATES"
    try:
        if env_name in os.environ:
            return max(1, int(os.environ[env_name]))
    except (TypeError, ValueError):
        pass
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    layout_cfg = metadata.get("layout", {})
    layout_cfg = layout_cfg if isinstance(layout_cfg, Mapping) else {}
    compact_cfg = layout_cfg.get("macro_refinement", {})
    compact_cfg = compact_cfg if isinstance(compact_cfg, Mapping) else {}
    block_cfg = compact_cfg.get(str(block), compact_cfg)
    block_cfg = block_cfg if isinstance(block_cfg, Mapping) else {}
    try:
        return max(1, int(block_cfg.get("max_candidates", 4)))
    except (TypeError, ValueError):
        return 4


def _macro_refinement_max_total_candidates(block: str, pdk: object | None) -> int:
    env_name = f"{str(block).upper()}_MACRO_REFINEMENT_MAX_TOTAL_CANDIDATES"
    try:
        if env_name in os.environ:
            return max(1, int(os.environ[env_name]))
    except (TypeError, ValueError):
        pass
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    layout_cfg = metadata.get("layout", {})
    layout_cfg = layout_cfg if isinstance(layout_cfg, Mapping) else {}
    compact_cfg = layout_cfg.get("macro_refinement", {})
    compact_cfg = compact_cfg if isinstance(compact_cfg, Mapping) else {}
    block_cfg = compact_cfg.get(str(block), compact_cfg)
    block_cfg = block_cfg if isinstance(block_cfg, Mapping) else {}
    try:
        return max(1, int(block_cfg.get("max_total_candidates", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _limit_macro_refinement_candidates(
    candidates: Sequence[MacroRefinementCandidateSpec],
    block: str,
    pdk: object | None,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Apply an optional total candidate cap while preserving candidate order.

    The order is intentional: baseline/current-guard candidates come first,
    followed by observation-driven local refinements, then broader ladder split
    variants.  A PDK can cap total candidates to keep Bandgap/LDO exploratory
    SMT from becoming a long multi-minute search while still giving the solver
    the most reference-inspired local packing options.
    """

    rows = tuple(candidates)
    include_env = f"{str(block).upper()}_MACRO_REFINEMENT_INCLUDE"
    include_names = {
        item.strip()
        for item in str(os.environ.get(include_env, "") or "").split(",")
        if item.strip()
    }
    if include_names:
        selected = tuple(candidate for candidate in rows if candidate.name in include_names)
        if selected:
            rows = selected
    max_total = _macro_refinement_max_total_candidates(block, pdk)
    if max_total <= 0 or len(rows) <= max_total:
        return rows
    return rows[:max_total]


def _flat_smt_env_positive_int(block: str, suffix: str) -> int | None:
    env_name = f"{str(block).upper()}_FLAT_SMT_{suffix}"
    if env_name not in os.environ:
        return None
    try:
        return max(1, int(os.environ[env_name]))
    except (TypeError, ValueError):
        return None


def _flat_smt_env_enabled(block: str, suffix: str) -> bool:
    env_name = f"{str(block).upper()}_FLAT_SMT_{suffix}"
    return _truthy_env(os.environ.get(env_name)) if env_name in os.environ else False


def _flat_refinement_score(
    compiled: object,
    missing_groups: Sequence[str],
    *,
    block: str = "",
    layout_proxy_score: int = 0,
    refinement: object | None = None,
) -> tuple[int, int, int, int, int, int, int]:
    checks = getattr(compiled, "checks", {}) or {}
    passed = bool(getattr(compiled, "passed", False)) and not tuple(missing_groups)
    width = int(getattr(compiled, "total_width_tracks", 0) or 0)
    height = int(getattr(compiled, "total_height_tracks", 0) or 0)
    area = width * height if width > 0 and height > 0 else 10**12
    objective = int(_mapping(checks).get("objective_score", area) or area)
    aesthetic_proxy = _flat_refinement_aesthetic_proxy_cost(
        width,
        height,
        block=block,
        layout_proxy_score=layout_proxy_score,
        refinement=refinement,
    )
    true_area_weight = int(_mapping(checks).get("true_area_weight", 0) or 0)
    if true_area_weight > 0:
        return (
            0 if passed else 1,
            len(tuple(missing_groups)),
            aesthetic_proxy,
            area,
            max(width, height) if width > 0 and height > 0 else 10**9,
            max(0, int(layout_proxy_score)),
            objective,
        )
    return (
        0 if passed else 1,
        len(tuple(missing_groups)),
        aesthetic_proxy,
        area,
        max(width, height) if width > 0 and height > 0 else 10**9,
        max(0, int(layout_proxy_score)),
        objective,
    )


def _flat_refinement_aesthetic_proxy_cost(
    width_tracks: int,
    height_tracks: int,
    *,
    block: str = "",
    layout_proxy_score: int = 0,
    refinement: object | None = None,
) -> int:
    """Return a candidate-level visual packing proxy for macro refinement.

    The SMT objective already optimizes each candidate internally.  This proxy
    ranks *between* macro-refinement candidates.  Raw area alone tends to select
    thin/tall strip layouts that are compact numerically but look unlike analog
    block layouts.  The proxy keeps area in the cost, then adds a configurable
    penalty for aspect imbalance and maximum side length.
    """

    width = max(0, int(width_tracks or 0))
    height = max(0, int(height_tracks or 0))
    if width <= 0 or height <= 0:
        return 10**12
    area = width * height
    aspect_num, aspect_den = _flat_refinement_target_aspect(refinement, block=block)
    aspect_error = abs(width * aspect_den - height * aspect_num)
    max_side = max(width, height)
    aspect_weight = _flat_refinement_aspect_weight(block)
    max_side_weight = _flat_refinement_max_side_weight(block)
    route_proxy_weight = _flat_refinement_route_proxy_weight(block)
    macro_risk = _flat_refinement_macro_risk_score(refinement, block=block)
    return int(
        area
        + aspect_weight * aspect_error
        + max_side_weight * max_side
        + route_proxy_weight * max(0, int(layout_proxy_score))
        + macro_risk
    )


def _flat_refinement_aesthetic_proxy_report(
    compiled: object,
    *,
    block: str = "",
    layout_proxy_score: int = 0,
    refinement: object | None = None,
) -> dict[str, int]:
    width = int(getattr(compiled, "total_width_tracks", 0) or 0)
    height = int(getattr(compiled, "total_height_tracks", 0) or 0)
    aspect_num, aspect_den = _flat_refinement_target_aspect(refinement, block=block)
    return {
        "width_tracks": width,
        "height_tracks": height,
        "area_tracks2": width * height if width > 0 and height > 0 else 10**12,
        "target_aspect_num": aspect_num,
        "target_aspect_den": aspect_den,
        "aspect_error_tracks": (
            abs(width * aspect_den - height * aspect_num)
            if width > 0 and height > 0
            else 10**9
        ),
        "max_side_tracks": max(width, height) if width > 0 and height > 0 else 10**9,
        "aspect_weight": _flat_refinement_aspect_weight(block),
        "max_side_weight": _flat_refinement_max_side_weight(block),
        "route_proxy_weight": _flat_refinement_route_proxy_weight(block),
        "route_proxy_score": max(0, int(layout_proxy_score)),
        "experimental_macro_risk": _flat_refinement_macro_risk_score(refinement, block=block),
        "cost": _flat_refinement_aesthetic_proxy_cost(
            width,
            height,
            block=block,
            layout_proxy_score=layout_proxy_score,
            refinement=refinement,
        ),
    }


def _flat_refinement_target_aspect(
    refinement: object | None,
    *,
    block: str = "",
) -> tuple[int, int]:
    spec = getattr(refinement, "spec", None) if refinement is not None else None
    objective = getattr(spec, "objective", None)
    if objective is not None:
        try:
            raw_num = int(getattr(objective, "aspect_num", 0) or 0)
            raw_den = int(getattr(objective, "aspect_den", 0) or 0)
            if raw_num > 0 and raw_den > 0:
                return raw_num, raw_den
        except (TypeError, ValueError):
            pass
    if str(block).lower() == "ldo":
        return 2, 1
    return 1, 1


def _flat_refinement_aspect_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "ASPECT_WEIGHT", default=32)


def _flat_refinement_max_side_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "MAX_SIDE_WEIGHT", default=2)


def _flat_refinement_route_proxy_weight(block: str) -> int:
    # LDO critical-net HPWL and route demand are already present inside every
    # SMT candidate.  Re-multiplying the corridor proxy by three at the outer
    # selection layer made it dominate the 2:1 block aspect and consistently
    # selected a square floorplan over a near-equal-area human-style power
    # island.  Keep one copy of the proxy for LDO candidate comparison.
    default = 1 if str(block).lower() == "ldo" else 3
    return _flat_refinement_env_nonnegative_int(block, "ROUTE_PROXY_WEIGHT", default=default)


def _flat_refinement_macro_risk_score(refinement: object | None, *, block: str = "") -> int:
    if refinement is None:
        return 0
    metadata = _mapping(getattr(refinement, "metadata", {}) or {})
    if bool(metadata.get("validated_physical_clean", False)):
        return 0
    block_upper = str(block or "").upper()
    allow_names = (
        f"{block_upper}_ALLOW_EXPERIMENTAL_MACRO_REFINEMENT" if block_upper else "",
        selected_env_name("ALLOW_EXPERIMENTAL_MACRO_REFINEMENT"),
    )
    if any(name and _truthy_env(os.environ.get(name)) for name in allow_names):
        return 0
    kind = str(metadata.get("kind", "") or "").lower()
    name = str(getattr(refinement, "name", "") or "").lower()
    if kind in {"", "baseline", "passive_realization_guard"} or name in {"baseline_macro", "current_passive_guard"}:
        return 0
    return _flat_refinement_env_nonnegative_int(block, "EXPERIMENTAL_MACRO_RISK", default=12_000)


def _flat_refinement_env_nonnegative_int(block: str, suffix: str, *, default: int) -> int:
    names = (
        f"{str(block).upper()}_FLAT_REFINEMENT_{suffix}",
        selected_env_name(f"FLAT_REFINEMENT_{suffix}"),
    )
    for name in names:
        if name not in os.environ:
            continue
        try:
            return max(0, int(os.environ[name]))
        except (TypeError, ValueError):
            continue
    return max(0, int(default))


def _flat_layout_proxy_score(
    group_bboxes_tracks: Mapping[str, tuple[int, int, int, int]],
    corridors: Sequence[HierarchicalRoutingCorridor2D],
    capacities: Mapping[str, int],
    *,
    block: str = "",
) -> dict[str, int]:
    """Return candidate-local packing facts used for lightweight scoring.

    This is not a detailed router.  It captures the reference-design facts that
    matter before streamout: local whitespace inside the candidate envelope and
    corridor proxy span.  Reference ADC/PLL layouts keep repeated primitive
    motifs near local rule minima, so candidates with large internal holes or
    long corridor proxies should lose even when their raw bbox area is similar.
    """

    rows = tuple(tuple(int(v) for v in bbox) for bbox in group_bboxes_tracks.values())
    if not rows:
        return {
            "device_envelope_area_tracks2": 10**12,
            "group_area_sum_tracks2": 0,
            "group_whitespace_tracks2": 10**12,
            "corridor_span_tracks": 10**9,
            "score": 10**12,
        }
    x0 = min(row[0] for row in rows)
    y0 = min(row[1] for row in rows)
    x1 = max(row[2] for row in rows)
    y1 = max(row[3] for row in rows)
    envelope_area = max(0, x1 - x0) * max(0, y1 - y0)
    group_area = sum(max(0, row[2] - row[0]) * max(0, row[3] - row[1]) for row in rows)
    whitespace = max(0, envelope_area - group_area)
    corridor_span = 0
    corridor_area = 0
    for corridor in corridors:
        source = group_bboxes_tracks.get(corridor.source_group)
        target = group_bboxes_tracks.get(corridor.target_group)
        if source is None or target is None:
            continue
        cap = max(1, int(capacities.get(corridor.name, corridor.base_capacity_tracks or 1)))
        cx0, cy0, cx1, cy1 = _flat_corridor_bbox_tracks(source, target, corridor.orientation, cap)
        corridor_span += max(0, cx1 - cx0) + max(0, cy1 - cy0)
        corridor_area += max(0, cx1 - cx0) * max(0, cy1 - cy0)
    empty_topology = _flat_empty_space_topology(rows, (x0, y0, x1, y1))
    whitespace_weight = _flat_layout_proxy_whitespace_weight(block)
    corridor_span_weight = _flat_layout_proxy_corridor_span_weight(block)
    corridor_area_weight = _flat_layout_proxy_corridor_area_weight(block)
    fragmented_weight = _flat_layout_proxy_fragmented_whitespace_weight(block)
    isolation_weight = _flat_layout_proxy_isolation_weight(block)
    ragged_edge_weight = _flat_layout_proxy_ragged_edge_weight(block)
    isolation_gap = _flat_group_isolation_gap(rows)
    fragmented_whitespace = int(empty_topology["fragmented_empty_tracks2"])
    ragged_edge = int(empty_topology["ragged_edge_tracks"])
    score = (
        whitespace_weight * whitespace
        + corridor_span_weight * corridor_span
        + corridor_area_weight * corridor_area
        + fragmented_weight * fragmented_whitespace
        + isolation_weight * isolation_gap
        + ragged_edge_weight * ragged_edge
    )
    return {
        "device_envelope_area_tracks2": int(envelope_area),
        "group_area_sum_tracks2": int(group_area),
        "group_whitespace_tracks2": int(whitespace),
        "group_whitespace_weight": int(whitespace_weight),
        "corridor_span_tracks": int(corridor_span),
        "corridor_span_weight": int(corridor_span_weight),
        "corridor_area_tracks2": int(corridor_area),
        "corridor_area_weight": int(corridor_area_weight),
        "empty_component_count": int(empty_topology["empty_component_count"]),
        "largest_empty_component_tracks2": int(empty_topology["largest_empty_component_tracks2"]),
        "fragmented_empty_tracks2": fragmented_whitespace,
        "fragmented_empty_weight": int(fragmented_weight),
        "ragged_edge_tracks": ragged_edge,
        "ragged_edge_weight": int(ragged_edge_weight),
        "isolation_gap_tracks": int(isolation_gap),
        "isolation_gap_weight": int(isolation_weight),
        "score": int(score),
    }


def _flat_layout_proxy_whitespace_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "LAYOUT_PROXY_WHITESPACE_WEIGHT", default=4)


def _flat_layout_proxy_corridor_span_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "LAYOUT_PROXY_CORRIDOR_SPAN_WEIGHT", default=12)


def _flat_layout_proxy_corridor_area_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "LAYOUT_PROXY_CORRIDOR_AREA_WEIGHT", default=1)


def _flat_layout_proxy_fragmented_whitespace_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "LAYOUT_PROXY_FRAGMENTED_WHITESPACE_WEIGHT", default=6)


def _flat_layout_proxy_isolation_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "LAYOUT_PROXY_ISOLATION_WEIGHT", default=10)


def _flat_layout_proxy_ragged_edge_weight(block: str) -> int:
    return _flat_refinement_env_nonnegative_int(block, "LAYOUT_PROXY_RAGGED_EDGE_WEIGHT", default=2)


def _flat_empty_space_topology(
    boxes: Sequence[tuple[int, int, int, int]],
    envelope: tuple[int, int, int, int],
) -> dict[str, int]:
    """Measure empty-space fragmentation and boundary raggedness on the SMT grid."""

    x0, y0, x1, y1 = (int(value) for value in envelope)
    width = max(0, x1 - x0)
    height = max(0, y1 - y0)
    if width <= 0 or height <= 0 or width * height > 250_000:
        return {
            "empty_component_count": 0,
            "largest_empty_component_tracks2": 0,
            "fragmented_empty_tracks2": 0,
            "ragged_edge_tracks": 0,
        }
    occupied: set[tuple[int, int]] = set()
    for bx0, by0, bx1, by1 in boxes:
        for xx in range(max(x0, bx0), min(x1, bx1)):
            for yy in range(max(y0, by0), min(y1, by1)):
                occupied.add((xx, yy))
    empty = {
        (xx, yy)
        for xx in range(x0, x1)
        for yy in range(y0, y1)
        if (xx, yy) not in occupied
    }
    component_sizes: list[int] = []
    while empty:
        seed = empty.pop()
        stack = [seed]
        size = 0
        while stack:
            xx, yy = stack.pop()
            size += 1
            for neighbor in ((xx - 1, yy), (xx + 1, yy), (xx, yy - 1), (xx, yy + 1)):
                if neighbor in empty:
                    empty.remove(neighbor)
                    stack.append(neighbor)
        component_sizes.append(size)
    largest = max(component_sizes, default=0)
    total_empty = sum(component_sizes)
    boundary_cells = {
        *((xx, y0) for xx in range(x0, x1)),
        *((xx, y1 - 1) for xx in range(x0, x1)),
        *((x0, yy) for yy in range(y0, y1)),
        *((x1 - 1, yy) for yy in range(y0, y1)),
    }
    ragged = sum(1 for cell in boundary_cells if cell not in occupied)
    return {
        "empty_component_count": len(component_sizes),
        "largest_empty_component_tracks2": largest,
        "fragmented_empty_tracks2": max(0, total_empty - largest),
        "ragged_edge_tracks": ragged,
    }


def _flat_group_isolation_gap(boxes: Sequence[tuple[int, int, int, int]]) -> int:
    """Return summed nearest-neighbour Manhattan gaps for visibly isolated groups."""

    if len(boxes) <= 1:
        return 0

    def axis_gap(a0: int, a1: int, b0: int, b1: int) -> int:
        if a1 < b0:
            return b0 - a1
        if b1 < a0:
            return a0 - b1
        return 0

    total = 0
    for index, a in enumerate(boxes):
        nearest = min(
            axis_gap(a[0], a[2], b[0], b[2]) + axis_gap(a[1], a[3], b[1], b[3])
            for other, b in enumerate(boxes)
            if other != index
        )
        total += max(0, nearest - 1)
    return total
def _layout_tweak_patch_id(patch: Mapping[str, Any]) -> str:
    try:
        return str(patch.get("patch_id", "") or "")
    except AttributeError:
        return str(getattr(patch, "patch_id", "") or "")


def _layout_tweak_patch_operation_count(patch: Mapping[str, Any]) -> int:
    try:
        raw_ops = patch.get("operations", ())  # type: ignore[attr-defined]
    except AttributeError:
        raw_ops = getattr(patch, "operations", ())
    try:
        return len(tuple(raw_ops or ()))
    except TypeError:
        return 0


def _layout_tweak_patch_op_count(patch: Mapping[str, Any], op_name: str) -> int:
    try:
        raw_ops = tuple(patch.get("operations", ()) or ())  # type: ignore[attr-defined]
    except AttributeError:
        raw_ops = tuple(getattr(patch, "operations", ()) or ())
    selected = str(op_name).lower()
    return sum(1 for op in raw_ops if _layout_tweak_operation_op(op) == selected)


def _placement_stage_layout_tweak_patch(patch: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if patch is None:
        return None
    try:
        raw_ops = tuple(patch.get("operations", ()) or ())  # type: ignore[attr-defined]
        patch_dict = dict(patch)
    except AttributeError:
        raw_ops = tuple(getattr(patch, "operations", ()) or ())
        patch_dict = {
            "patch_id": getattr(patch, "patch_id", ""),
            "baseline_layout_id": getattr(patch, "baseline_layout_id", ""),
            "observation_refs": tuple(getattr(patch, "observation_refs", ()) or ()),
            "operations": raw_ops,
            "acceptance": getattr(patch, "acceptance", {}),
            "notes": getattr(patch, "notes", ""),
        }
    filtered = tuple(op for op in raw_ops if _layout_tweak_operation_op(op) != "route_lane")
    if len(filtered) == len(raw_ops):
        return patch
    patch_dict["operations"] = filtered
    patch_dict["notes"] = (
        str(patch_dict.get("notes", ""))
        + f"\nRoute-lane operations deferred to structured routing stage: {len(raw_ops) - len(filtered)}."
    ).strip()
    return patch_dict


def apply_layout_tweak_route_resources_to_smt_result(
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
    patch: Mapping[str, Any] | None,
) -> AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult:
    """Inject route-lane tweak operations after placement SMT is fixed.

    Route feedback is a routing-stage ECO.  Applying it before flat macro
    selection can unintentionally alter the layout candidate choice.  This
    helper keeps placement fixed and only appends DSL route-resource rows for
    ``_build_structured_interconnect_plan`` to consume.
    """

    resources = _layout_tweak_route_resources_from_patch(patch)
    if not resources:
        return smt_result
    checks = dict(getattr(smt_result, "checks", {}) or {})
    existing = tuple(checks.get("dsl_route_resources", ()) or ())
    checks["dsl_route_resources"] = tuple((*existing, *resources))
    checks["route_stage_layout_tweak_patch_applied"] = True
    checks["route_stage_layout_tweak_route_resource_count"] = len(resources)
    checks["route_stage_layout_tweak_patch_id"] = _layout_tweak_patch_id(patch or {})
    return replace(smt_result, checks=checks)


def _layout_tweak_route_resources_from_patch(patch: Mapping[str, Any] | None) -> tuple[dict[str, object], ...]:
    if patch is None:
        return ()
    try:
        raw_ops = tuple(patch.get("operations", ()) or ())  # type: ignore[attr-defined]
        patch_id = str(patch.get("patch_id", "") or "")  # type: ignore[attr-defined]
    except AttributeError:
        raw_ops = tuple(getattr(patch, "operations", ()) or ())
        patch_id = str(getattr(patch, "patch_id", "") or "")
    resources: list[dict[str, object]] = []
    for index, op in enumerate(raw_ops):
        if _layout_tweak_operation_op(op) != "route_lane":
            continue
        route_name = _layout_tweak_operation_value(op, "route_name") or _layout_tweak_operation_value(op, "target")
        if not route_name:
            continue
        metadata = dict(_mapping(_layout_tweak_operation_value(op, "metadata")))
        row: dict[str, object] = {
            "name": str(route_name),
            "match": str(metadata.get("match", "net") or "net"),
            "layer": str(_layout_tweak_operation_value(op, "layer") or ""),
            "lane": _layout_tweak_operation_value(op, "lane"),
            "source": f"layout_tweak_route_stage:{patch_id}:{index}",
        }
        for key in (
            "allowed_layers",
            "forbidden_layers",
            "cyclic_layers",
            "cyclic_lanes",
            "avoid_nets",
            "avoid_prefixes",
            "route_policy",
            "style",
            "orientation",
            "channel_orientation",
            "trunk_orientation",
            "channel_side",
            "side",
            "channel_offset_um",
            "channel_offset_nm",
            "dogleg_side",
            "dogleg_offset_um",
            "dogleg_offset_nm",
            "dogleg_offset_step_um",
            "dogleg_offset_step_nm",
            "terminal_escape_style",
            "escape_style",
            "terminal_escape_um",
            "terminal_escape_nm",
            "prefer_horizontal",
            "prefer_vertical",
        ):
            value = metadata.get(key)
            if value is not None and value != "":
                row[key] = value
        resources.append(row)
    return tuple(resources)


def _layout_tweak_operation_op(operation: object) -> str:
    if isinstance(operation, Mapping):
        return str(operation.get("op", "") or "").lower()
    return str(getattr(operation, "op", "") or "").lower()


def _layout_tweak_operation_value(operation: object, key: str) -> object:
    if isinstance(operation, Mapping):
        return operation.get(key)
    return getattr(operation, key, None)


def _run_flat_compact_smt(
    block: str,
    graph: TopologyGraph,
    base: AnalogHierarchicalSmtResult,
    group_specs: Mapping[str, AnalogSmtGroupSpec],
    *,
    pdk: object | None,
    device_sizes_um: DeviceSizeMap,
    sizing: Mapping[str, Mapping[str, object]] | None,
    track_pitch_um: float,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
    layout_tweak_patch: Mapping[str, Any] | None = None,
    solver_timeout_ms: int | None = None,
    max_candidate_count: int | None = None,
    max_refinement_candidates: int | None = None,
) -> AnalogFlatCompactSmtResult:
    if block == "ldo":
        from analogskills.blocks.layout_specs.ldo import make_ldo_layout_spec

        spec = make_ldo_layout_spec(graph)
    elif block == "bandgap":
        from analogskills.blocks.layout_specs.bandgap import make_bandgap_layout_spec

        spec = make_bandgap_layout_spec(graph)
    else:
        raise ValueError(f"unsupported flat compact analog SMT block: {block}")
    spec = _apply_configured_layout_relation_overrides(spec, pdk, block)
    spec = _apply_configured_layout_pack_overrides(spec, pdk, block)
    spec = _apply_shared_sd_readiness_from_pdk(spec, graph, pdk, block)
    spec = _apply_configured_guard_ring_and_dummy_policy(spec, pdk, block)
    layout_tweak_patch_id = ""
    layout_tweak_patch_operation_count = 0
    layout_tweak_route_lane_operation_count = 0
    if layout_tweak_patch is not None:
        layout_tweak_patch_id = _layout_tweak_patch_id(layout_tweak_patch)
        layout_tweak_patch_operation_count = _layout_tweak_patch_operation_count(layout_tweak_patch)
        layout_tweak_route_lane_operation_count = _layout_tweak_patch_op_count(layout_tweak_patch, "route_lane")
        placement_patch = _placement_stage_layout_tweak_patch(layout_tweak_patch)
        if placement_patch is not None:
            spec = apply_layout_tweak_patch_to_spec(spec, placement_patch)
    true_area_weight_override = _flat_smt_env_positive_int(block, "TRUE_AREA_WEIGHT")
    if true_area_weight_override is not None:
        spec = replace(
            spec,
            objective=replace(
                spec.objective,
                true_area_weight=int(true_area_weight_override),
            ),
        )
    pcell_realization_smt_disabled = _flat_smt_env_enabled(block, "DISABLE_PCELL_REALIZATIONS")
    if not pcell_realization_smt_disabled:
        spec = _apply_auto_pcell_realization_groups(
            spec,
            graph,
            sizing,
            pdk,
            block,
            calibration_cache=calibration_cache,
            pcell_calibre_catalog=pcell_calibre_catalog,
        )
    rule_strategy = _analog_rule_strategy_for_result(base, block, pdk)
    solver_timeout_override_ms = (
        max(1, int(solver_timeout_ms))
        if solver_timeout_ms is not None
        else _flat_smt_env_positive_int(block, "TIMEOUT_MS")
    )
    max_candidate_override = (
        max(1, int(max_candidate_count))
        if max_candidate_count is not None
        else _flat_smt_env_positive_int(block, "MAX_CANDIDATES")
    )
    rule_solver_timeout_ms = solver_timeout_override_ms or _positive_int(rule_strategy.get("timeout_ms", 15_000), 15_000)
    rule_max_candidate_count = max_candidate_override or _positive_int(rule_strategy.get("max_candidate_count", 256), 256)
    solver_timeout_ms = rule_solver_timeout_ms
    max_candidate_count = rule_max_candidate_count
    pcell_realization_group_count = len(tuple(getattr(spec, "pcell_realization_groups", ()) or ()))
    if pcell_realization_group_count:
        solver_timeout_ms = min(max(solver_timeout_ms, 5_000), 30_000)
        max_candidate_count = min(max_candidate_count, 64)

    refinement_candidates = _macro_refinement_candidates_for_flat_smt(block, spec, graph, pdk)
    if max_refinement_candidates is not None:
        refinement_candidates = refinement_candidates[: max(1, int(max_refinement_candidates))]
    compiled_rows = []
    for refinement in refinement_candidates:
        refinement_pcell_group_count = len(tuple(getattr(refinement.spec, "pcell_realization_groups", ()) or ()))
        refinement_solver_timeout_ms = solver_timeout_ms
        refinement_max_candidate_count = max_candidate_count
        if pcell_realization_group_count and refinement_pcell_group_count == 0:
            # A macro refinement that freezes PCell realizations no longer has
            # the large realization-choice search space that motivated the
            # 30s cap.  Use the configured rule-strategy timeout for these
            # placement-only candidates; otherwise valid compact solutions can
            # be dropped as timeout/unknown and misreported as unsat.
            refinement_solver_timeout_ms = rule_solver_timeout_ms
            refinement_max_candidate_count = min(rule_max_candidate_count, 64)
        refinement_kind = str(_mapping(refinement.metadata or {}).get("kind", "") or "")
        if refinement_kind in {"void_insertion_refinement", "split_void_insertion_refinement"}:
            # Observation-driven void insertion is a narrow but harder local
            # packing solve.  z3 often needs more than the default/capped 30s
            # to prove the compact solution; shorter runs return unknown and
            # are reported as unsat by the current compiler contract.
            if solver_timeout_override_ms is None:
                refinement_solver_timeout_ms = max(refinement_solver_timeout_ms, 120_000)
            refinement_max_candidate_count = min(refinement_max_candidate_count, 64)
        compiled_row = compile_analog_layout_smt(
            refinement.spec,
            graph,
            device_sizes_um=device_sizes_um,
            track_pitch_um=track_pitch_um,
            placement_spacing_um=_flat_compact_spacing_um(base, refinement.spec, track_pitch_um),
            max_candidate_count=refinement_max_candidate_count,
            solver_timeout_ms=refinement_solver_timeout_ms,
        )
        aggregate_bboxes = aggregate_macro_bboxes_tracks(
            compiled_row.pattern_bboxes_tracks,
            refinement.macro_subpatterns,
        )
        candidate_group_bboxes = {
            name: aggregate_bboxes[name]
            for name in group_specs
            if name in aggregate_bboxes
        }
        candidate_missing_groups = tuple(sorted(set(group_specs) - set(candidate_group_bboxes)))
        layout_proxy = _flat_layout_proxy_score(
            candidate_group_bboxes,
            base.problem.corridors,
            base.physical.master.corridor_capacity_tracks,
            block=block,
        )
        score = _flat_refinement_score(
            compiled_row,
            candidate_missing_groups,
            block=block,
            layout_proxy_score=int(layout_proxy.get("score", 0) or 0),
            refinement=refinement,
        )
        compiled_rows.append(
            (
                refinement,
                compiled_row,
                candidate_group_bboxes,
                candidate_missing_groups,
                score,
                layout_proxy,
                refinement_solver_timeout_ms,
                refinement_max_candidate_count,
            )
        )

    selection_rows = compiled_rows
    if pcell_realization_group_count:
        pcell_realized_rows = [
            row
            for row in compiled_rows
            if bool(row[1].passed)
            and not tuple(row[3])
            and _positive_int(_mapping(row[1].checks).get("selected_pcell_realization_count", 0), 0) > 0
        ]
        if pcell_realized_rows:
            selection_rows = pcell_realized_rows
    selected_refinement, compiled, group_bboxes_tracks, missing_groups, _selected_refinement_score = min(
        selection_rows,
        key=lambda row: row[4],
    )[:5]
    refinement_diagnostics = tuple(
        {
            "name": refinement.name,
            "passed": bool(compiled_row.passed and not candidate_missing_groups),
            "missing_groups": tuple(candidate_missing_groups),
            "score": tuple(int(item) for item in score),
            "total_width_tracks": int(compiled_row.total_width_tracks),
            "total_height_tracks": int(compiled_row.total_height_tracks),
            "estimated_area_tracks": int(compiled_row.total_width_tracks) * int(compiled_row.total_height_tracks),
            "group_bboxes_tracks": {
                str(name): tuple(int(v) for v in bbox)
                for name, bbox in dict(_candidate_group_bboxes).items()
            },
            "layout_proxy": dict(layout_proxy),
            "aesthetic_proxy": _flat_refinement_aesthetic_proxy_report(
                compiled_row,
                block=block,
                layout_proxy_score=int(layout_proxy.get("score", 0) or 0),
                refinement=refinement,
            ),
            "objective_score": int(_mapping(compiled_row.checks).get("objective_score", 0) or 0),
            "solver_timeout_ms": int(_mapping(compiled_row.checks).get("solver_timeout_ms", 0) or used_solver_timeout_ms),
            "max_candidate_count": int(used_max_candidate_count),
            "selected_candidates": dict(_mapping(compiled_row.checks).get("selected_candidates", {}) or {}),
            "selected_relations": dict(_mapping(compiled_row.checks).get("selected_relations", {}) or {}),
            "hard_positional_relation_count": sum(
                1
                for relation in tuple(getattr(refinement.spec, "relations", ()) or ())
                if bool(getattr(relation, "hard", True))
                and str(getattr(relation, "kind", "")).lower() in {"above", "below", "left_of", "right_of"}
            ),
            "soft_positional_relation_count": sum(
                1
                for relation in tuple(getattr(refinement.spec, "relations", ()) or ())
                if not bool(getattr(relation, "hard", True))
                and str(getattr(relation, "kind", "")).lower() in {"above", "below", "left_of", "right_of"}
            ),
            "notes": refinement.notes,
            "metadata": dict(refinement.metadata or {}),
        }
        for (
            refinement,
            compiled_row,
            _candidate_group_bboxes,
            candidate_missing_groups,
            score,
            layout_proxy,
            used_solver_timeout_ms,
            used_max_candidate_count,
        ) in compiled_rows
    )
    corridor_bboxes_um = _flat_corridor_bboxes_um(
        base.problem.corridors,
        group_bboxes_tracks,
        base.physical.master.corridor_capacity_tracks,
        track_pitch_um,
    )
    raw_issues = compiled.checks.get("issues", ())
    issues = list(raw_issues if isinstance(raw_issues, tuple) else ())
    if missing_groups:
        issues.append(f"flat compact SMT missing groups: {missing_groups}")
    checks = {
        **dict(compiled.checks),
        "passed": bool(compiled.passed and not missing_groups),
        "issues": tuple(issues),
        "block": block,
        "group_count": len(group_specs),
        "device_count": len(graph.devices),
        "total_width_tracks": compiled.total_width_tracks,
        "total_height_tracks": compiled.total_height_tracks,
        "routing_capacity_base": "hierarchical_smt",
        "configuration_path": spec.drc.rule_profile_path,
        "critical_route_count": len(base.problem.critical_routes),
        "noncritical_route_count": len(base.problem.noncritical_routes),
        "corridor_capacity_tracks": dict(base.physical.master.corridor_capacity_tracks),
        "critical_load_by_corridor": dict(base.physical.master.critical_load_by_corridor),
        "smt_mode": str(rule_strategy.get("mode", "hybrid")),
        "smt_solver_timeout_ms": int(compiled.checks.get("solver_timeout_ms", solver_timeout_ms) or solver_timeout_ms),
        "smt_max_candidate_count": max_candidate_count,
        "flat_smt_timeout_override_ms": solver_timeout_override_ms,
        "flat_smt_max_candidate_override": max_candidate_override,
        "flat_smt_true_area_weight_override": true_area_weight_override,
        "flat_smt_pcell_realization_smt_disabled": bool(pcell_realization_smt_disabled),
        "layout_tweak_patch_applied": layout_tweak_patch is not None,
        "layout_tweak_patch_id": layout_tweak_patch_id,
        "layout_tweak_patch_operation_count": layout_tweak_patch_operation_count,
        "layout_tweak_route_lane_operation_count": layout_tweak_route_lane_operation_count,
        "layout_tweak_route_lane_stage": "structured_routing",
        "macro_refinement_candidate_count": len(refinement_candidates),
        "selected_macro_refinement": selected_refinement.name,
        "selected_macro_refinement_notes": selected_refinement.notes,
        "selected_macro_refinement_metadata": dict(selected_refinement.metadata or {}),
        "selected_macro_hard_positional_relation_count": sum(
            1
            for relation in tuple(getattr(selected_refinement.spec, "relations", ()) or ())
            if bool(getattr(relation, "hard", True))
            and str(getattr(relation, "kind", "")).lower() in {"above", "below", "left_of", "right_of"}
        ),
        "macro_refinement_candidate_diagnostics": refinement_diagnostics,
        "macro_subpatterns": {
            str(name): tuple(str(item) for item in subpatterns)
            for name, subpatterns in selected_refinement.macro_subpatterns.items()
        },
        "pcell_realization_mode": str(compiled.checks.get("pattern_choice_mode", "disabled"))
        if pcell_realization_group_count
        else "disabled",
        "rule_strategy": dict(rule_strategy),
        "rule_family_owners": dict(_mapping(rule_strategy.get("rule_family_owners", {}))),
        "rule_owner_schema_version": rule_strategy.get("owner_schema_version"),
        "main_smt_hard_rule_families": tuple(rule_strategy.get("main_smt_hard_rule_families", ()) or ()),
        "main_smt_proxy_rule_families": tuple(rule_strategy.get("main_smt_proxy_rule_families", ()) or ()),
        "main_smt_rule_families": tuple(rule_strategy.get("main_smt_rule_families", ()) or ()),
        "local_smt_rule_families": tuple(rule_strategy.get("local_smt_rule_families", ()) or ()),
        "a_star_rule_families": tuple(rule_strategy.get("a_star_rule_families", ()) or ()),
        "eco_rule_families": tuple(rule_strategy.get("eco_rule_families", ()) or ()),
        "signoff_only_rule_families": tuple(rule_strategy.get("signoff_only_rule_families", ()) or ()),
        "external_eco_rule_families": tuple(rule_strategy.get("external_eco_rule_families", ()) or ()),
        "ignored_rule_families": tuple(rule_strategy.get("ignored_rule_families", ()) or ()),
    }
    return AnalogFlatCompactSmtResult(
        block,
        graph,
        base,
        tuple(compiled.placements),
        group_bboxes_tracks,
        corridor_bboxes_um,
        track_pitch_um,
        checks,
        routing_origin="flat_compact_smt_structured",
    )


def _apply_configured_guard_ring_and_dummy_policy(
    spec: object,
    pdk: object | None,
    block: str,
) -> object:
    """Load guard-ring and native MOS dummy policy from the active PDK.

    No geometry dimensions are owned by the block DSL.  The DSL only carries
    the resolved contract so the compiler, reports, and OA lowering use one
    source of truth.
    """

    if pdk is None or not hasattr(spec, "drc"):
        return spec
    from analogskills.layout.power import configured_guard_ring_geometry

    guard = configured_guard_ring_geometry(pdk, block=block)  # type: ignore[arg-type]
    metadata = _metadata(pdk)
    sweep = _mapping(_mapping(metadata.get("pcell_drc_sweep", {})).get("strongarm_mos", {}))
    finger_rules = _mapping(sweep.get("mos_finger_constraints", {}))
    selected_variant = str(finger_rules.get("variant", "") or "")
    variant_params: Mapping[str, object] = {}
    for row in tuple(sweep.get("variants", ()) or ()):
        item = _mapping(row)
        if str(item.get("name", "") or "") == selected_variant:
            variant_params = _mapping(item.get("params", {}))
            break
    required_dummy_params = tuple(
        sorted(
            key
            for key in variant_params
            if "dummy" in str(key).lower() or str(key) == "MatchDpoWithGate"
        )
    )
    return replace(
        spec,
        drc=replace(
            spec.drc,
            guard_ring_enabled=bool(guard.get("enabled", False)),
            guard_ring_net=str(guard.get("net", "VSS") or "VSS"),
            guard_ring_kind=str(guard.get("kind", "substrate") or "substrate"),
            guard_ring_width_um=max(0.0, float(guard.get("width_um", 0.0) or 0.0)),
            guard_ring_spacing_um=max(0.0, float(guard.get("spacing_um", 0.0) or 0.0)),
            guard_ring_contact_pitch_um=max(1e-6, float(guard.get("contact_pitch_um", 1.0) or 1.0)),
            guard_ring_extra_spacing_um_by_side={
                str(side): max(0.0, float(value or 0.0))
                for side, value in _mapping(guard.get("extra_spacing_um_by_side", {})).items()
            },
            matched_mos_dummy_policy="native_pcell_edge" if required_dummy_params else "none",
            matched_mos_dummy_required_params=required_dummy_params,
        ),
    )


def _flat_compact_spacing_um(base: AnalogHierarchicalSmtResult, spec: object, track_pitch_um: float) -> float:
    pitch = max(float(track_pitch_um), 1e-6)
    try:
        configured = getattr(spec, "drc").placement_spacing_um
        if configured is not None:
            return max(float(configured), pitch)
    except Exception:
        pass
    return max(int(base.problem.placement_spacing_tracks) * pitch, pitch)


def _apply_auto_pcell_realization_groups(
    spec: object,
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, object]] | None,
    pdk: object | None,
    block: str,
    *,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
) -> object:
    if not sizing:
        return spec
    groups = _auto_pcell_realization_groups(
        graph,
        sizing,
        pdk,
        block,
        calibration_cache=calibration_cache,
        pcell_calibre_catalog=pcell_calibre_catalog,
    )
    if not groups:
        return spec
    existing = tuple(getattr(spec, "pcell_realization_groups", ()) or ())
    explicitly_realized_devices = {
        str(device)
        for group in existing
        for device in tuple(getattr(group, "devices", ()) or ())
        if str(device)
    }
    if explicitly_realized_devices:
        filtered_groups = []
        for group in groups:
            devices = tuple(str(device) for device in tuple(getattr(group, "devices", ()) or ()) if str(device))
            if any(device in explicitly_realized_devices for device in devices):
                continue
            filtered_groups.append(group)
        groups = tuple(filtered_groups)
        if not groups:
            return spec
    return type(spec)(
        block=spec.block,
        patterns=spec.patterns,
        pairs=spec.pairs,
        relations=spec.relations,
        critical_nets=spec.critical_nets,
        route_resources=spec.route_resources,
        pack_constraints=spec.pack_constraints,
        placement_windows=getattr(spec, "placement_windows", ()),
        objective_terms=spec.objective_terms,
        pcell_realization_groups=existing + groups,
        noncritical_router=spec.noncritical_router,
        objective=spec.objective,
        drc=spec.drc,
        notes=spec.notes,
    )


def _apply_shared_sd_readiness_from_pdk(
    spec: object,
    graph: TopologyGraph,
    pdk: object | None,
    block: str,
) -> object:
    """Attach calibrated shared-S/D readiness contracts to matching DSL pairs.

    The base layout specs intentionally express only intent.  The PDK metadata
    decides whether a true physical shared-diffusion realization is available
    for a specific device logical/topology.  If no ready contract matches, the
    compiler keeps the pair in proximity-only mode.
    """

    cfg = _shared_diffusion_realization_config(pdk)
    if not cfg or not _bool_like(cfg.get("enabled", False)):
        return spec
    candidates = _shared_diffusion_candidate_rows(cfg)
    if not candidates:
        return spec
    updated_pairs = []
    changed = False
    for pair in tuple(getattr(spec, "pairs", ()) or ()):
        if not bool(getattr(pair, "shared_sd", False)):
            updated_pairs.append(pair)
            continue
        existing = getattr(pair, "shared_sd_readiness", {}) or {}
        if isinstance(existing, Mapping) and existing:
            updated_pairs.append(pair)
            continue
        contract = _shared_diffusion_contract_for_pair(pair, graph, candidates, block=block)
        if not contract:
            updated_pairs.append(pair)
            continue
        updated_pairs.append(replace(pair, shared_sd_readiness=contract))
        changed = True
    if not changed:
        return spec
    return replace(spec, pairs=tuple(updated_pairs))


def _shared_diffusion_realization_config(pdk: object | None) -> Mapping[str, object]:
    metadata = _metadata(pdk)
    direct = dict(_mapping(metadata.get("shared_diffusion_realization", {})))
    pcell = _mapping(metadata.get("pcell_realization", {}))
    mos = _mapping(pcell.get("mos", {}))
    nested = dict(_mapping(mos.get("shared_diffusion", {})))
    if direct and nested:
        merged = {**direct, **nested}
        if "candidates" in direct or "candidates" in nested:
            merged["candidates"] = {
                **dict(_mapping(direct.get("candidates", {}))),
                **dict(_mapping(nested.get("candidates", {}))),
            }
        return merged
    return nested or direct


def _shared_diffusion_candidate_rows(cfg: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw = cfg.get("candidates", ())
    rows: list[Mapping[str, object]] = []
    if isinstance(raw, Mapping):
        for name, value in sorted(raw.items()):
            if isinstance(value, Mapping):
                row = dict(value)
                row.setdefault("candidate", str(name))
                rows.append(row)
    else:
        for value in tuple(raw or ()):
            if isinstance(value, Mapping):
                rows.append(dict(value))
    if rows:
        return tuple(
            sorted(
                rows,
                key=lambda row: (
                    -_positive_int(row.get("priority", row.get("selection_priority", 0)), 0),
                    str(row.get("candidate", "")),
                ),
            )
        )
    return (dict(cfg),)


def _shared_diffusion_contract_for_pair(
    pair: object,
    graph: TopologyGraph,
    candidates: Sequence[Mapping[str, object]],
    *,
    block: str,
) -> dict[str, object]:
    left = str(getattr(pair, "left", "") or "")
    right = str(getattr(pair, "right", "") or "")
    if left not in graph.devices or right not in graph.devices:
        return {}
    left_logical = _mos_logical_name_for_device(graph.devices[left])
    right_logical = _mos_logical_name_for_device(graph.devices[right])
    if left_logical != right_logical or left_logical not in {"nmos", "pmos"}:
        return {}
    pair_role = str(getattr(pair, "role", "") or "")
    shared_role = str(getattr(pair, "shared_sd_role", "") or "")
    for row in candidates:
        if not _shared_diffusion_candidate_matches_pair(
            row,
            logical=left_logical,
            pair_role=pair_role,
            shared_role=shared_role,
            block=block,
        ):
            continue
        contract = dict(row)
        contract.setdefault("source", "pdk_metadata.shared_diffusion_realization")
        contract.setdefault("logical", left_logical)
        contract.setdefault("status", "not_evaluated")
        contract.setdefault("solver_allowed_mode", "proximity_only")
        contract.setdefault("physical_diffusion_merge_allowed", False)
        return {str(key): value for key, value in contract.items()}
    return {}


def _shared_diffusion_candidate_matches_pair(
    row: Mapping[str, object],
    *,
    logical: str,
    pair_role: str,
    shared_role: str,
    block: str,
) -> bool:
    logicals = _string_tuple(row.get("allowed_device_logicals", row.get("logicals", row.get("logical", ()))))
    if logicals and str(logical) not in logicals:
        return False
    roles = _string_tuple(row.get("pair_roles", row.get("roles", ())))
    if roles and str(pair_role) not in roles:
        return False
    shared_roles = _string_tuple(row.get("shared_sd_roles", row.get("terminal_roles", ())))
    if shared_roles and str(shared_role) not in shared_roles:
        return False
    blocks = _string_tuple(row.get("blocks", ()))
    if blocks and str(block) not in blocks:
        return False
    if "enabled" in row and not _bool_like(row.get("enabled")):
        return False
    return True


def _string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    try:
        return tuple(str(item) for item in tuple(value) if str(item))
    except TypeError:
        return (str(value),) if str(value) else ()


def _auto_pcell_realization_groups(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, object]],
    pdk: object | None,
    block: str,
    *,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
) -> tuple[object, ...]:
    from analogskills.layout.analog_layout_dsl import PCellRealizationGroupSpec

    groups: list[PCellRealizationGroupSpec] = []
    mos_devices = tuple(
        name
        for name, device in graph.devices.items()
        if _mos_logical_name_for_device(device) in {"nmos", "pmos"}
    )
    for devices in _matched_or_singleton_mos_groups(mos_devices, sizing):
        candidates = _mos_pcell_realization_candidates(
            graph,
            devices[0],
            sizing,
            pdk,
            calibration_cache=calibration_cache,
            pcell_calibre_catalog=pcell_calibre_catalog,
        )
        if candidates:
            groups.append(
                PCellRealizationGroupSpec(
                    f"pcell_mos_{'_'.join(devices)}",
                    tuple(devices),
                    candidates,
                    True,
                    "auto-generated MOS nf/m realization candidates for main flat SMT",
                )
            )

    bjt_devices = tuple(
        name
        for name, device in graph.devices.items()
        if _logical_pcell_name_for_device(device) == "bjt"
    )
    for devices in _same_bjt_sizing_groups(bjt_devices, sizing):
        candidates = _bjt_pcell_realization_candidates(
            graph,
            devices[0],
            sizing,
            pdk,
            calibration_cache=calibration_cache,
            pcell_calibre_catalog=pcell_calibre_catalog,
        )
        if candidates:
            groups.append(
                PCellRealizationGroupSpec(
                    f"pcell_bjt_{'_'.join(devices)}",
                    tuple(devices),
                    candidates,
                    True,
                    "auto-generated BJT native PCell realization candidates for main flat SMT",
                )
            )

    passive_groups = _passive_realization_device_groups(graph, block)
    for group_name, devices in passive_groups:
        candidates = _passive_pcell_realization_candidates(
            graph,
            devices[0],
            sizing,
            pdk,
            calibration_cache=calibration_cache,
            pcell_calibre_catalog=pcell_calibre_catalog,
        )
        if candidates:
            groups.append(
                PCellRealizationGroupSpec(
                    f"pcell_{group_name}",
                    tuple(devices),
                    candidates,
                    True,
                    "auto-generated passive aspect-ratio candidates for main flat SMT",
                )
            )
    return tuple(groups)


def _logical_pcell_name_for_device(device: object) -> str:
    try:
        from analogskills.pcell.generation import logical_pcell_name

        return str(logical_pcell_name(device)).lower()
    except Exception:
        return _mos_logical_name_for_device(device)


def _matched_or_singleton_mos_groups(
    mos_devices: Sequence[str],
    sizing: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[str, ...], ...]:
    remaining = set(str(name) for name in mos_devices)
    groups: list[tuple[str, ...]] = []
    for name in sorted(mos_devices):
        if name not in remaining:
            continue
        if name.endswith("A"):
            mate = name[:-1] + "B"
            if mate in remaining and _same_mos_electrical_size(sizing.get(name, {}), sizing.get(mate, {})):
                groups.append((name, mate))
                remaining.remove(name)
                remaining.remove(mate)
                continue
        groups.append((name,))
        remaining.remove(name)
    return tuple(groups)


def _same_bjt_sizing_groups(
    bjt_devices: Sequence[str],
    sizing: Mapping[str, Mapping[str, object]],
) -> tuple[tuple[str, ...], ...]:
    buckets: dict[tuple[str, str], list[str]] = {}
    for name in sorted(str(item) for item in bjt_devices):
        row = _mapping(sizing.get(name, {}))
        mult = str(row.get("M", row.get("m", 1)) or 1)
        model = str(row.get("model", "") or "")
        buckets.setdefault((model, mult), []).append(name)
    return tuple(tuple(items) for _key, items in sorted(buckets.items(), key=lambda item: (item[0], item[1])))


def _same_mos_electrical_size(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    lw = _sizing_dimension_m(left, ("W", "w", "width"), 1e-6)
    rw = _sizing_dimension_m(right, ("W", "w", "width"), 1e-6)
    ll = _sizing_dimension_m(left, ("L", "l", "length"), 0.18e-6)
    rl = _sizing_dimension_m(right, ("L", "l", "length"), 0.18e-6)
    return abs(lw - rw) <= max(lw, rw, 1e-12) * 1e-9 and abs(ll - rl) <= max(ll, rl, 1e-12) * 1e-9


def _mos_pcell_realization_candidates(
    graph: TopologyGraph,
    device_name: str,
    sizing: Mapping[str, Mapping[str, object]],
    pdk: object | None,
    *,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
) -> tuple[object, ...]:
    from analogskills.layout.analog_layout_dsl import pcell_candidate
    from analogskills.pcell.generation import rank_mos_finger_layout_candidates

    device = graph.devices[str(device_name)]
    row = dict(sizing.get(str(device_name), {}) or {})
    width_m = _sizing_dimension_m(row, ("W", "w", "width"), 1e-6)
    length_m = _sizing_dimension_m(row, ("L", "l", "length"), 0.18e-6)
    if width_m <= 0.0 or length_m <= 0.0:
        return ()
    rules = _crn28_mos_finger_rule_config(pdk) if pdk is not None else {}
    logical = _mos_logical_name_for_device(device)
    mos_cfg = _pcell_realization_device_config(pdk, "mos")
    logical_mos_cfg = _mapping(mos_cfg.get(logical, {}))
    if logical_mos_cfg:
        mos_cfg = {**dict(mos_cfg), **dict(logical_mos_cfg)}
    top_k = _positive_int(mos_cfg.get("top_k", 8), 8)
    min_finger_m = max(float(rules.get("min_finger_width_nm", 500.0) or 500.0) * 1e-9, 1e-12)
    max_by_logical = _mapping(rules.get("max_finger_width_nm_by_logical", {}))
    max_finger_nm = float(max_by_logical.get(logical, max_by_logical.get("default", 10_000.0)) or 10_000.0)
    max_finger_m = max(max_finger_nm * 1e-9, min_finger_m)
    max_nf = max(1, int(rules.get("max_nf", 64) or 64))
    max_m = max(1, int(rules.get("max_m", 16) or 16))
    prefer_even_nf = bool(rules.get("prefer_even_nf", False))
    config_pcell_overrides = _crn28_mos_config_pcell_overrides(pdk, logical, rules)
    ranked = rank_mos_finger_layout_candidates(
        width_m=width_m,
        length_m=length_m,
        objective="balanced",
        min_finger_width_m=min_finger_m,
        max_finger_width_m=max_finger_m,
        max_fingers=max_nf,
        max_multiplier=max_m,
        top_k=top_k,
    )
    rows: list[tuple[int, int, float, float, float, int, str]] = []
    current_nf = max(1, int(row.get("nf", 1) or 1))
    current_m = max(1, int(row.get("m", 1) or 1))
    current_wf = width_m / max(current_nf * current_m, 1)
    current_width_um, current_height_um = _estimate_device_bbox_um(graph, str(device_name), row, current_nf, current_m, current_wf)
    current_respects_config = _mos_finger_choice_respects_config(
        current_nf,
        current_m,
        current_wf,
        min_finger_m=min_finger_m,
        max_finger_m=max_finger_m,
        max_nf=max_nf,
        max_m=max_m,
    )
    try:
        max_growth_when_current_legal = max(float(mos_cfg.get("max_growth_when_current_legal", 1.60) or 1.60), 1.0)
    except (TypeError, ValueError):
        max_growth_when_current_legal = 1.60
    if current_respects_config:
        rows.append((current_nf, current_m, current_wf, current_width_um, current_height_um, 0, "current_config_legal"))
    else:
        legal_nf, legal_m, legal_wf = _legal_max_finger_realization(
            width_m,
            max_finger_m=max_finger_m,
            max_nf=max_nf,
            max_m=max_m,
            prefer_even=prefer_even_nf,
        )
        legal_width_um, legal_height_um = _estimate_device_bbox_um(
            graph,
            str(device_name),
            row,
            legal_nf,
            legal_m,
            legal_wf,
        )
        rows.append((legal_nf, legal_m, legal_wf, legal_width_um, legal_height_um, 1, "config_legal_maxw"))
    for index, candidate in enumerate(ranked):
        choice = candidate.choice
        if not _mos_finger_choice_respects_config(
            int(choice.nf),
            int(choice.m),
            float(choice.finger_width_m),
            min_finger_m=min_finger_m,
            max_finger_m=max_finger_m,
            max_nf=max_nf,
            max_m=max_m,
        ):
            continue
        if current_respects_config and float(candidate.width_um) > current_width_um * max_growth_when_current_legal:
            continue
        rows.append(
            (
                int(choice.nf),
                int(choice.m),
                float(choice.finger_width_m),
                float(candidate.width_um),
                float(candidate.height_um),
                4 + index,
                "config_ranked",
            )
        )
    deduped: dict[tuple[int, int], tuple[int, int, float, float, float, int, str]] = {}
    for row_tuple in rows:
        key = (row_tuple[0], row_tuple[1])
        existing = deduped.get(key)
        if existing is None or row_tuple[5] < existing[5]:
            deduped[key] = row_tuple
    result = []
    require_calibrated = bool(mos_cfg.get("require_calibrated", False))
    allow_nearest_calibration = bool(mos_cfg.get("allow_nearest_calibration", False))
    for nf, mult, wf, width_um, height_um, cost, source in sorted(deduped.values(), key=lambda item: (item[5], item[3] * item[4], item[0], item[1])):
        sizing_overrides = {
            "nf": int(nf),
            "m": int(mult),
            "wf": float(wf),
            "layout_width_um": float(width_um),
            "layout_height_um": float(height_um),
            "native_pcell_realization": True,
            "pcell_realization_kind": "mos_nf_m",
            "pcell_realization_source": "config_constrained_estimated",
            "mos_dynamic_generation_policy": "config_constrained",
            "mos_min_finger_width_m": float(min_finger_m),
            "mos_max_finger_width_m": float(max_finger_m),
        }
        candidate_sizing = {**row, **sizing_overrides}
        calibrated = _calibrated_bbox_um_for_sizing(
            graph,
            str(device_name),
            candidate_sizing,
            pdk,
            calibration_cache,
            allow_nearest=allow_nearest_calibration,
        )
        drc_clean = True
        lvs_clean = True
        note_suffix = "estimated bbox"
        if calibrated is not None:
            width_um, height_um, match_policy, clean = calibrated
            sizing_overrides["layout_width_um"] = float(width_um)
            sizing_overrides["layout_height_um"] = float(height_um)
            sizing_overrides["pcell_realization_source"] = f"calibration_{match_policy}"
            sizing_overrides["calibrated_pcell_realization"] = True
            drc_clean = bool(clean)
            lvs_clean = bool(clean)
            note_suffix = f"calibrated bbox ({match_policy})"
        elif require_calibrated:
            continue
        calibre_status = _calibre_catalog_status_for_sizing(
            graph,
            str(device_name),
            candidate_sizing,
            pdk,
            pcell_calibre_catalog,
        )
        if calibre_status is not None:
            status, usable = calibre_status
            sizing_overrides["pcell_calibre_status"] = status
            sizing_overrides["pcell_calibre_usable_for_layout"] = bool(usable)
            drc_clean = bool(drc_clean and usable)
            lvs_clean = bool(lvs_clean and usable)
            note_suffix = f"{note_suffix}; Calibre catalog status={status}"
        footprint_bbox = (
            _mos_generated_access_footprint_bbox_um(
                graph,
                str(device_name),
                {**candidate_sizing, "pcell_overrides": config_pcell_overrides},
                pdk,
                width_um=width_um,
                height_um=height_um,
            )
            if _mos_access_aware_footprint_enabled(pdk)
            else None
        )
        if footprint_bbox is not None:
            bbox_x0, bbox_y0, bbox_x1, bbox_y1 = footprint_bbox
            width_um = max(float(bbox_x1) - float(bbox_x0), 1e-9)
            height_um = max(float(bbox_y1) - float(bbox_y0), 1e-9)
            sizing_overrides["layout_width_um"] = float(width_um)
            sizing_overrides["layout_height_um"] = float(height_um)
            sizing_overrides["layout_bbox_x0_um"] = float(bbox_x0)
            sizing_overrides["layout_bbox_y0_um"] = float(bbox_y0)
            sizing_overrides["pcell_realization_footprint"] = "native_pcell_plus_generated_access"
        candidate_name = f"{source}_nf{nf}_m{mult}"
        metadata = _pcell_candidate_metadata(
            logical_name=logical,
            candidate_name=candidate_name,
            width_um=width_um,
            height_um=height_um,
            sizing_overrides=sizing_overrides,
            pcell_overrides=config_pcell_overrides,
            realization_kind="mos_nf_m",
            source=str(sizing_overrides.get("pcell_realization_source", "")),
            clean=bool(drc_clean and lvs_clean),
        )
        metadata.update(
            _mos_human_reference_metadata(
                logical=logical,
                total_width_m=width_m,
                length_m=length_m,
                nf=int(nf),
                mult=int(mult),
                is_unit_array=False,
                mos_cfg=mos_cfg,
            )
        )
        result.append(
            pcell_candidate(
                candidate_name,
                width_um,
                height_um,
                sizing_overrides=sizing_overrides,
                pcell_overrides=config_pcell_overrides,
                cost=int(cost),
                drc_clean=drc_clean,
                lvs_clean=lvs_clean,
                notes=f"MOS realization candidate generated from fixed electrical W/L under configured finger constraints; {note_suffix}",
                metadata=metadata,
            )
        )
    array_candidates = _mos_unit_array_pcell_candidates(
        graph,
        str(device_name),
        row,
        logical,
        width_m,
        length_m,
        min_finger_m=min_finger_m,
        max_finger_m=max_finger_m,
        max_nf=max_nf,
        config_pcell_overrides=config_pcell_overrides,
        mos_cfg=mos_cfg,
        pdk=pdk,
    ) if not _mos_unit_array_no_split_requested(device, row) else ()
    if array_candidates:
        combined = tuple(result) + tuple(array_candidates)
        return tuple(sorted(combined, key=_pcell_candidate_smt_objective_sort_key)[:top_k])
    return tuple(sorted(result, key=_pcell_candidate_smt_objective_sort_key)[:top_k])


def _legal_max_finger_realization(
    width_m: float,
    *,
    max_finger_m: float,
    max_nf: int,
    max_m: int,
    prefer_even: bool = False,
) -> tuple[int, int, float]:
    """Return a DRC-oriented nf/m split near the maximum legal finger width."""

    width = max(float(width_m), 1e-12)
    max_finger = max(float(max_finger_m), 1e-12)
    nf = max(1, int(ceil(width / max_finger)))
    if prefer_even and nf > 1 and nf % 2:
        nf += 1
    mult = 1
    if nf > max(1, int(max_nf)):
        max_nf_int = max(1, int(max_nf))
        mult = max(1, int(ceil(nf / max_nf_int)))
        mult = min(mult, max(1, int(max_m)))
        nf = min(max_nf_int, max(1, int(ceil(width / (max_finger * mult)))))
        if prefer_even and nf > 1 and nf % 2 and nf < max_nf_int:
            nf += 1
    wf = width / max(nf * mult, 1)
    return max(1, int(nf)), max(1, int(mult)), max(wf, 1e-12)


def _mos_unit_array_pcell_candidates(
    graph: TopologyGraph,
    device_name: str,
    row: Mapping[str, object],
    logical: str,
    width_m: float,
    length_m: float,
    *,
    min_finger_m: float,
    max_finger_m: float,
    max_nf: int,
    config_pcell_overrides: Mapping[str, object],
    mos_cfg: Mapping[str, object],
    pdk: object | None,
) -> tuple[object, ...]:
    from analogskills.layout.analog_layout_dsl import pcell_candidate

    array_cfg = _mos_unit_array_config_for_device(graph, device_name, logical, mos_cfg, sizing=row)
    if array_cfg and not _bool_like(array_cfg.get("enabled", True)):
        return ()
    effective_mos_cfg = {**dict(mos_cfg), "unit_array": array_cfg}
    target_unit_width_um = _mos_reference_unit_width_um(logical, effective_mos_cfg)
    total_width_um = max(float(width_m) * 1e6, 1e-6)
    trigger = max(target_unit_width_um * 1.35, target_unit_width_um + 0.25)
    if total_width_um < trigger:
        return ()
    base_count = max(2, int(ceil(total_width_um / max(target_unit_width_um, 1e-6))))
    max_units = _positive_int(array_cfg.get("max_unit_count", mos_cfg.get("max_unit_array_count", 8)), 8)
    base_count = min(base_count, max_units)
    raw_counts = tuple(array_cfg.get("unit_counts", ()) or ())
    counts: list[int] = []
    for raw in (*raw_counts, base_count, 2, 4, 8):
        try:
            count = max(2, int(float(raw)))
        except (TypeError, ValueError):
            continue
        if count <= max_units and total_width_um / count >= min_finger_m * 1e6 * 0.5:
            counts.append(count)
    counts = list(dict.fromkeys(counts))
    spacing_um = _positive_float(array_cfg.get("spacing_um", mos_cfg.get("unit_array_spacing_um", 0.5)), 0.5)
    spacing_x_um = _positive_float(array_cfg.get("spacing_x_um", spacing_um), spacing_um)
    spacing_y_um = _positive_float(array_cfg.get("spacing_y_um", spacing_um), spacing_um)
    access_pitch_margin_x_um = _nonnegative_float(array_cfg.get("access_pitch_margin_x_um", 0.0), 0.0)
    access_pitch_margin_y_um = _nonnegative_float(array_cfg.get("access_pitch_margin_y_um", 0.0), 0.0)
    result = []
    seen: set[tuple[int, int, int]] = set()
    for unit_count in counts:
        unit_total_width_m = max(float(width_m) / float(unit_count), 1e-12)
        unit_nf, unit_m, unit_wf = _legal_max_finger_realization(
            unit_total_width_m,
            max_finger_m=max_finger_m,
            max_nf=max_nf,
            max_m=1,
            prefer_even=False,
        )
        if not _mos_finger_choice_respects_config(
            unit_nf,
            unit_m,
            unit_wf,
            min_finger_m=min_finger_m,
            max_finger_m=max_finger_m,
            max_nf=max_nf,
            max_m=1,
        ):
            continue
        unit_row = {**dict(row), "W": unit_total_width_m, "L": float(length_m)}
        unit_width_um, unit_height_um = _estimate_device_bbox_um(
            graph,
            device_name,
            unit_row,
            unit_nf,
            unit_m,
            unit_wf,
        )
        unit_footprint_bbox = (
            _mos_generated_access_footprint_bbox_um(
                graph,
                device_name,
                {
                    **unit_row,
                    "nf": int(unit_nf),
                    "m": int(unit_m),
                    "wf": float(unit_wf),
                    "pcell_overrides": config_pcell_overrides,
                },
                pdk,
                width_um=unit_width_um,
                height_um=unit_height_um,
            )
            if _mos_access_aware_footprint_enabled(pdk)
            else None
        )
        if unit_footprint_bbox is None:
            unit_bbox_x0_um = 0.0
            unit_bbox_y0_um = 0.0
        else:
            unit_bbox_x0_um, unit_bbox_y0_um, unit_bbox_x1_um, unit_bbox_y1_um = unit_footprint_bbox
            unit_width_um = max(float(unit_bbox_x1_um) - float(unit_bbox_x0_um), 1e-9)
            unit_height_um = max(float(unit_bbox_y1_um) - float(unit_bbox_y0_um), 1e-9)
        for rows, cols in _factor_pairs(unit_count):
            key = (unit_count, rows, cols)
            if key in seen:
                continue
            seen.add(key)
            pitch_x_um = unit_width_um + spacing_x_um + access_pitch_margin_x_um
            pitch_y_um = unit_height_um + spacing_y_um + access_pitch_margin_y_um
            layout_width_um = unit_width_um + max(0, cols - 1) * pitch_x_um
            layout_height_um = unit_height_um + max(0, rows - 1) * pitch_y_um
            aspect_penalty = abs(rows - cols)
            unit_width_ratio = unit_total_width_m * 1e6 / max(target_unit_width_um, 1e-6)
            width_overflow_weight = _positive_float(array_cfg.get("unit_width_overflow_cost_per_ratio", 2.0), 2.0)
            nf_overflow_weight = _positive_float(array_cfg.get("unit_nf_overflow_cost_per_finger", 1.0), 1.0)
            unit_width_overflow_cost = max(0, int(round((unit_width_ratio - 1.25) * width_overflow_weight)))
            unit_nf_overflow_cost = max(0, int(round((max(0, int(unit_nf) - 8)) * nf_overflow_weight)))
            array_spec = {
                "enabled": True,
                "logical_name": str(logical),
                "unit_count": int(unit_count),
                "rows": int(rows),
                "cols": int(cols),
                "unit_total_width_m": float(unit_total_width_m),
                "unit_width_m": float(unit_total_width_m),
                "unit_length_m": float(length_m),
                "unit_nf": int(unit_nf),
                "unit_m": int(unit_m),
                "unit_finger_width_m": float(unit_wf),
                "unit_width_um": float(unit_width_um),
                "unit_height_um": float(unit_height_um),
                "unit_layout_bbox_x0_um": float(unit_bbox_x0_um),
                "unit_layout_bbox_y0_um": float(unit_bbox_y0_um),
                "target_unit_width_um": float(target_unit_width_um),
                "unit_width_ratio_to_target": float(unit_width_ratio),
                "spacing_um": float(spacing_um),
                "spacing_x_um": float(spacing_x_um),
                "spacing_y_um": float(spacing_y_um),
                "access_pitch_margin_x_um": float(access_pitch_margin_x_um),
                "access_pitch_margin_y_um": float(access_pitch_margin_y_um),
                "pitch_x_um": float(pitch_x_um),
                "pitch_y_um": float(pitch_y_um),
                "array_access_envelope_model": "bbox_plus_configured_access_pitch_margin",
                "parallel_reduction_expected": True,
                "human_reference_style": "finite_primitive_unit_array",
                "reference_design_basis": "ADC/PLL reference designs use repeated primitive MOS shapes and LVS parallel reduction",
            }
            sizing_overrides = {
                "W": float(width_m),
                "L": float(length_m),
                "nf": int(unit_nf),
                "m": int(unit_m),
                "wf": float(unit_wf),
                "layout_width_um": float(layout_width_um),
                "layout_height_um": float(layout_height_um),
                "layout_bbox_x0_um": 0.0,
                "layout_bbox_y0_um": 0.0,
                "native_pcell_realization": True,
                "pcell_realization_kind": "mos_unit_array",
                "pcell_realization_source": "human_reference_unit_array",
                "mos_dynamic_generation_policy": "config_constrained",
                "mos_unit_array": array_spec,
                "mos_min_finger_width_m": float(min_finger_m),
                "mos_max_finger_width_m": float(max_finger_m),
            }
            candidate_name = f"human_ref_array_M{unit_count}_{rows}x{cols}_nf{unit_nf}"
            metadata = _pcell_candidate_metadata(
                logical_name=logical,
                candidate_name=candidate_name,
                width_um=layout_width_um,
                height_um=layout_height_um,
                sizing_overrides=sizing_overrides,
                pcell_overrides=config_pcell_overrides,
                realization_kind="mos_unit_array",
                source="human_reference_unit_array",
                clean=True,
            )
            metadata.update(
                _mos_human_reference_metadata(
                    logical=logical,
                    total_width_m=width_m,
                    length_m=length_m,
                    nf=unit_nf,
                    mult=unit_m,
                    is_unit_array=True,
                    unit_count=unit_count,
                    rows=rows,
                    cols=cols,
                    mos_cfg=effective_mos_cfg,
                )
            )
            metadata["unit_width_overflow_cost"] = int(unit_width_overflow_cost)
            metadata["unit_nf_overflow_cost"] = int(unit_nf_overflow_cost)
            metadata["dfm_cost"] = int(metadata.get("dfm_cost", 0) or 0) + unit_width_overflow_cost + unit_nf_overflow_cost
            result.append(
                pcell_candidate(
                    candidate_name,
                    layout_width_um,
                    layout_height_um,
                    sizing_overrides=sizing_overrides,
                    pcell_overrides=config_pcell_overrides,
                    cost=int(10 + unit_count + aspect_penalty * 4 + unit_width_overflow_cost),
                    drc_clean=True,
                    lvs_clean=True,
                    notes="MOS unit-array candidate based on reference-design finite primitive repetition and LVS parallel reduction.",
                    metadata=metadata,
                )
            )
    return tuple(sorted(result, key=lambda candidate: (int(candidate.cost), float(candidate.width_um) * float(candidate.height_um), str(candidate.name))))


def _mos_unit_array_config_for_device(
    graph: TopologyGraph,
    device_name: str,
    logical: str,
    mos_cfg: Mapping[str, object],
    *,
    sizing: Mapping[str, object] | None = None,
) -> Mapping[str, object]:
    base = dict(_mapping(mos_cfg.get("unit_array", mos_cfg.get("mos_unit_array", {}))))
    device = graph.devices[str(device_name)]
    role = str(getattr(getattr(device, "role", ""), "value", getattr(device, "role", ""))).lower()
    params = _mapping(getattr(device, "params", {}))
    sizing_row = _mapping(sizing or {})
    overlays: list[Mapping[str, object]] = []
    for raw in (
        _mapping(base.get("logical_overrides", {})).get(str(logical).lower()),
        _mapping(base.get("role_overrides", {})).get(role),
        _mapping(base.get("device_overrides", {})).get(str(device_name)),
        _mapping(params.get("mos_unit_array", params.get("unit_array", {}))),
        _mapping(sizing_row.get("mos_unit_array", sizing_row.get("unit_array", {}))),
    ):
        row = _mapping(raw)
        if row:
            overlays.append(row)
    result = dict(base)
    for overlay in overlays:
        for key, value in overlay.items():
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = {**dict(_mapping(result.get(key))), **dict(value)}
            else:
                result[key] = value
    if _mos_unit_array_no_split_requested(device, sizing_row):
        result["enabled"] = False
        result["no_split"] = True
        result.setdefault("reason", "single_pcell_preserved_by_device_intent")
    return result


def _mos_unit_array_no_split_requested(device: object, sizing: Mapping[str, object] | None = None) -> bool:
    """Return True when circuit/layout intent forbids splitting one MOS into unit-array instances."""

    params = _mapping(getattr(device, "params", {}))
    sizing_row = _mapping(sizing or {})
    no_split_keys = (
        "preserve_single_pcell",
        "power_mos_atomic",
        "atomic_pcell",
        "no_split",
        "no_unit_array",
        "disable_unit_array",
        "disable_mos_unit_array",
    )
    for source in (params, sizing_row):
        for key in no_split_keys:
            if key in source and _bool_like(source[key]):
                return True
        array_cfg = _mapping(source.get("mos_unit_array", source.get("unit_array", {})))
        if array_cfg and "enabled" in array_cfg and not _bool_like(array_cfg["enabled"]):
            return True
    return False


def _mos_human_reference_metadata(
    *,
    logical: str,
    total_width_m: float,
    length_m: float,
    nf: int,
    mult: int,
    is_unit_array: bool,
    mos_cfg: Mapping[str, object],
    unit_count: int = 1,
    rows: int = 1,
    cols: int = 1,
) -> dict[str, object]:
    target_um = _mos_reference_unit_width_um(logical, mos_cfg)
    total_width_um = max(float(total_width_m) * 1e6, 1e-6)
    reference_units = max(1, int(ceil(total_width_um / max(target_um, 1e-6))))
    oversized_units = 0 if is_unit_array else max(0, reference_units - 1)
    row_col_imbalance = abs(int(rows) - int(cols)) if is_unit_array else 0
    return {
        "human_reference_style": "finite_primitive_unit_array",
        "reference_design_basis": "ADC/PLL reference designs use a finite MOS primitive set and LVS parallel reduction",
        "reference_unit_width_um": float(target_um),
        "electrical_total_width_um": float(total_width_um),
        "electrical_length_um": float(length_m) * 1e6,
        "native_nf": int(nf),
        "native_m": int(mult),
        "reference_unit_count_estimate": int(reference_units),
        "oversized_native_mos": bool(oversized_units > 0),
        "route_access_cost": (0 if is_unit_array else oversized_units * 20) + (max(0, min(int(rows), int(cols)) - 1) if is_unit_array else 0),
        "fragmentation_cost": max(0, int(unit_count) - 4) if is_unit_array else oversized_units * 5,
        "regularity_cost": row_col_imbalance if is_unit_array else 0,
        "array_cost": max(0, int(unit_count) - 1) if is_unit_array else 0,
    }


def _mos_reference_unit_width_um(logical: str, mos_cfg: Mapping[str, object]) -> float:
    array_cfg = _mapping(mos_cfg.get("unit_array", mos_cfg.get("mos_unit_array", {})))
    by_logical = _mapping(array_cfg.get("target_unit_width_um_by_logical", mos_cfg.get("target_unit_width_um_by_logical", {})))
    default_by_logical = {"nmos": 1.5, "pmos": 2.0}
    raw = by_logical.get(str(logical).lower(), array_cfg.get("target_unit_width_um", mos_cfg.get("target_unit_width_um")))
    return _positive_float(raw, default_by_logical.get(str(logical).lower(), 1.5))


def _pcell_candidate_smt_objective_sort_key(candidate: object) -> tuple[int, float, str]:
    metadata = dict(getattr(candidate, "metadata", {}) or {})
    try:
        total = max(0, int(getattr(candidate, "cost", 0) or 0))
    except (TypeError, ValueError):
        total = 0
    for key in (
        "shape_cost",
        "aspect_cost",
        "topology_cost",
        "array_cost",
        "regularity_cost",
        "route_access_cost",
        "fragmentation_cost",
        "pin_access_cost",
        "dfm_cost",
    ):
        try:
            total += max(0, int(metadata.get(key, 0) or 0))
        except (TypeError, ValueError):
            continue
    try:
        area = float(getattr(candidate, "width_um", 0.0) or 0.0) * float(getattr(candidate, "height_um", 0.0) or 0.0)
    except (TypeError, ValueError):
        area = 0.0
    return (total, area, str(getattr(candidate, "name", "")))


def _mos_finger_choice_respects_config(
    nf: int,
    mult: int,
    finger_width_m: float,
    *,
    min_finger_m: float,
    max_finger_m: float,
    max_nf: int,
    max_m: int,
) -> bool:
    try:
        nf_i = max(1, int(nf))
        mult_i = max(1, int(mult))
        wf = float(finger_width_m)
    except (TypeError, ValueError):
        return False
    if nf_i > max(1, int(max_nf)) or mult_i > max(1, int(max_m)):
        return False
    return wf >= float(min_finger_m) - 1e-18 and wf <= float(max_finger_m) + 1e-18


def _crn28_mos_config_pcell_overrides(
    pdk: object | None,
    logical: str,
    rules: Mapping[str, object],
) -> dict[str, object]:
    if pdk is None or str(getattr(pdk, "name", "")).lower() != "crn28hpcp":
        return {}
    by_logical = _crn28_mos_pcell_overrides(pdk, str(rules.get("variant", "") or ""))
    return dict(_mapping(by_logical.get(str(logical).lower(), {})))


def _estimate_device_bbox_um(
    graph: TopologyGraph,
    device_name: str,
    sizing: Mapping[str, object],
    nf: int,
    mult: int,
    wf: float,
) -> tuple[float, float]:
    from analogskills.pcell.generation import estimate_pcell_bbox_um

    row = dict(sizing)
    row.update({"nf": int(nf), "m": int(mult), "wf": float(wf)})
    return estimate_pcell_bbox_um(graph.devices[str(device_name)], row)


def _bjt_pcell_realization_candidates(
    graph: TopologyGraph,
    device_name: str,
    sizing: Mapping[str, Mapping[str, object]],
    pdk: object | None,
    *,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
) -> tuple[object, ...]:
    from analogskills.layout.analog_layout_dsl import pcell_candidate
    from analogskills.pcell.generation import estimate_pcell_bbox_um

    device = graph.devices[str(device_name)]
    if _logical_pcell_name_for_device(device) != "bjt":
        return ()
    row = dict(sizing.get(str(device_name), {}) or {})
    current_m = max(1, int(row.get("M", row.get("m", 1)) or 1))
    cfg = _pcell_realization_device_config(pdk, "bjt")
    configured = tuple(_mapping(item) for item in tuple(cfg.get("candidates", ()) or ()))
    if not configured:
        configured = (
            {
                "name": f"current_M{current_m}",
                "sizing_overrides": {"M": current_m},
                "cost": 0,
            },
        )
    preserve_electrical_m = bool(cfg.get("preserve_electrical_m", True))
    require_calibrated = bool(cfg.get("require_calibrated", False))
    allow_nearest_calibration = bool(cfg.get("allow_nearest_calibration", False))

    result = []
    seen: set[tuple[str, int, int]] = set()
    for index, item in enumerate(configured):
        pcell_overrides = _pcell_overrides_from_realization_config(item)
        sizing_overrides = dict(_mapping(item.get("sizing_overrides", {})))
        if "M" in item and "M" not in sizing_overrides:
            sizing_overrides["M"] = item.get("M")
        if "m" in item and "m" not in sizing_overrides:
            sizing_overrides["m"] = item.get("m")
        candidate_row = {**row, **sizing_overrides}
        if pcell_overrides:
            existing_pcell = dict(_mapping(candidate_row.get("pcell_overrides", {})))
            candidate_row["pcell_overrides"] = {**existing_pcell, **pcell_overrides}
        candidate_m = max(1, int(candidate_row.get("M", candidate_row.get("m", current_m)) or current_m))
        if preserve_electrical_m and candidate_m != current_m:
            continue
        width_um, height_um = _configured_candidate_bbox_um(item)
        if width_um <= 0.0 or height_um <= 0.0:
            width_um, height_um = estimate_pcell_bbox_um(device, candidate_row)
        calibrated = _calibrated_bbox_um_for_sizing(
            graph,
            str(device_name),
            candidate_row,
            pdk,
            calibration_cache,
            allow_nearest=allow_nearest_calibration,
        )
        drc_clean = True
        lvs_clean = True
        source = "configured_native" if item else "current_native"
        exported_clean_realization = bool(item.get("calibrated_pcell_realization", False)) or bool(
            item.get("pcell_calibre_usable_for_layout", False)
        )
        if calibrated is not None:
            width_um, height_um, match_policy, clean = calibrated
            source = f"calibration_{match_policy}"
            drc_clean = bool(clean)
            lvs_clean = bool(clean)
        elif exported_clean_realization:
            source = "exported_calibre_catalog"
            drc_clean = bool(item.get("drc_clean", True))
            lvs_clean = bool(item.get("lvs_clean", True))
        elif require_calibrated:
            continue
        calibre_status = _calibre_catalog_status_for_sizing(
            graph,
            str(device_name),
            candidate_row,
            pdk,
            pcell_calibre_catalog,
        )
        if calibre_status is not None:
            status, usable = calibre_status
            drc_clean = bool(drc_clean and usable)
            lvs_clean = bool(lvs_clean and usable)
        else:
            status, usable = "", True
        key = (
            str(_mapping(pcell_overrides)),
            round(float(width_um) * 1000),
            round(float(height_um) * 1000),
        )
        if key in seen:
            continue
        seen.add(key)
        sizing_overrides.update(
            {
                "M": candidate_m,
                "layout_width_um": float(width_um),
                "layout_height_um": float(height_um),
                "native_pcell_realization": True,
                "pcell_realization_kind": "bjt_native",
                "pcell_realization_source": source,
                "calibrated_pcell_realization": source.startswith("calibration_") or exported_clean_realization,
                "pcell_calibre_status": status,
                "pcell_calibre_usable_for_layout": bool(usable),
            }
        )
        candidate_name = str(item.get("name", f"bjt_M{candidate_m}_{index}"))
        result.append(
            pcell_candidate(
                candidate_name,
                float(width_um),
                float(height_um),
                sizing_overrides=sizing_overrides,
                pcell_overrides=pcell_overrides,
                cost=int(item.get("cost", index) or index),
                drc_clean=drc_clean,
                lvs_clean=lvs_clean,
                notes="BJT realization candidate generated from native PCell configuration/calibration",
                metadata=_pcell_candidate_metadata(
                    logical_name="bjt",
                    candidate_name=candidate_name,
                    width_um=float(width_um),
                    height_um=float(height_um),
                    sizing_overrides=sizing_overrides,
                    pcell_overrides=pcell_overrides,
                    realization_kind="bjt_native",
                    source=source,
                    clean=bool(drc_clean and lvs_clean),
                ),
            )
        )
    if current_m > 1 and pdk is not None:
        try:
            from analogskills.pcell.unit_library import bjt_unit_array_candidates_for_m

            for unit_candidate in bjt_unit_array_candidates_for_m(pdk, current_m, clean_only=True):
                smt_candidate = unit_candidate.to_smt_candidate_spec()
                key = (
                    str(_mapping(getattr(smt_candidate, "pcell_overrides", {}) or {})),
                    round(float(getattr(smt_candidate, "width_um", 0.0)) * 1000),
                    round(float(getattr(smt_candidate, "height_um", 0.0)) * 1000),
                )
                if key in seen:
                    continue
                seen.add(key)
                result.append(smt_candidate)
        except Exception:
            pass
    return tuple(result)


def _passive_realization_device_groups(
    graph: TopologyGraph,
    block: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    groups: list[tuple[str, tuple[str, ...]]] = []
    if block == "bandgap":
        resistors = tuple(sorted((name for name in graph.devices if name == "R1" or name.startswith("R2_")), key=_natural_suffix_key))
        if resistors:
            groups.append(("bandgap_resistor_ladder", resistors))
    elif block == "ldo":
        feedback = tuple(name for name in ("RFB_TOP", "RFB_BOT", "R1", "R2") if name in graph.devices)
        if feedback:
            groups.append(("ldo_feedback_resistors", feedback))
        if "RCOMP" in graph.devices:
            groups.append(("ldo_compensation_resistor", ("RCOMP",)))
        if "CCOMP" in graph.devices:
            groups.append(("ldo_compensation_cap", ("CCOMP",)))
        elif "COUT" in graph.devices:
            groups.append(("ldo_output_cap", ("COUT",)))
    return tuple(groups)


def _passive_pcell_realization_candidates(
    graph: TopologyGraph,
    device_name: str,
    sizing: Mapping[str, Mapping[str, object]],
    pdk: object | None,
    *,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
) -> tuple[object, ...]:
    from analogskills.layout.analog_layout_dsl import pcell_candidate
    from analogskills.pcell.generation import estimate_pcell_bbox_um, logical_pcell_name

    device = graph.devices[str(device_name)]
    logical = logical_pcell_name(device)
    if logical not in {"resistor", "capacitor"}:
        return ()
    row = dict(sizing.get(str(device_name), {}) or {})
    width_um, height_um = estimate_pcell_bbox_um(device, row)
    if width_um <= 0.0 or height_um <= 0.0:
        return ()
    drawn_passive = bool(row.get("use_drawn_primitive", row.get("use_drawn_passive_primitive", False)))
    if not drawn_passive:
        return _native_passive_pcell_realization_candidates(
            graph,
            str(device_name),
            logical,
            row,
            width_um,
            height_um,
            pdk,
            calibration_cache=calibration_cache,
            pcell_calibre_catalog=pcell_calibre_catalog,
        )
    area = max(width_um * height_um, 1e-12)
    side = area ** 0.5
    variants = [("current", width_um, height_um, 0)]
    passive_aspect_enabled = _passive_aspect_candidates_are_enabled(row, pdk)
    if passive_aspect_enabled:
        # The first compactness failure observed on the Brokaw bandgap is a
        # one-cell-width vertical whitespace strip caused by the 3x3 resistor
        # ladder rounding to 70 tracks while the BJT core is 65 tracks.  A small
        # width-trim candidate is safe for drawn passive primitives and gives
        # the main SMT a discrete choice that can remove that strip without
        # changing the resistor-ladder pattern.
        if drawn_passive:
            variants.append(("width_trim", max(width_um * 0.94, 0.2), height_um, 1))
        variants.extend(
            (
                ("rotated", height_um, width_um, 5),
                ("square", side, side, 8),
                ("narrow_tall", max(width_um * 0.5, 0.2), height_um * 2.0, 10),
                ("wide_short", width_um * 2.0, max(height_um * 0.5, 0.2), 12),
            )
        )
    result = []
    seen: set[tuple[int, int]] = set()
    for name, w, h, cost in variants:
        if not _passive_candidate_is_terminal_access_safe(logical, row, pdk, float(w), float(h)):
            continue
        key = (round(float(w) * 1000), round(float(h) * 1000))
        if key in seen:
            continue
        seen.add(key)
        sizing_overrides = {
            "layout_width_um": float(w),
            "layout_height_um": float(h),
            "layout_aspect_candidate": name != "current",
            "use_drawn_primitive": drawn_passive,
            "allow_pcell_aspect_candidates": passive_aspect_enabled,
            "pcell_realization_kind": f"{logical}_drawn",
            "pcell_realization_source": "drawn_passive_aspect_candidate",
        }
        candidate_name = f"{logical}_{name}"
        result.append(
            pcell_candidate(
                candidate_name,
                float(w),
                float(h),
                sizing_overrides=sizing_overrides,
                cost=int(cost),
                notes=f"{logical} aspect candidate generated from fixed passive sizing",
                metadata=_pcell_candidate_metadata(
                    logical_name=logical,
                    candidate_name=candidate_name,
                    width_um=float(w),
                    height_um=float(h),
                    sizing_overrides=sizing_overrides,
                    realization_kind=f"{logical}_drawn",
                    source="drawn_passive_aspect_candidate",
                    clean=True,
                ),
            )
        )
    return tuple(result)


def _native_passive_pcell_realization_candidates(
    graph: TopologyGraph,
    device_name: str,
    logical: str,
    row: Mapping[str, object],
    current_width_um: float,
    current_height_um: float,
    pdk: object | None,
    *,
    calibration_cache: object | None = None,
    pcell_calibre_catalog: object | None = None,
) -> tuple[object, ...]:
    from analogskills.layout.analog_layout_dsl import pcell_candidate

    cfg = _pcell_realization_device_config(pdk, logical)
    configured = tuple(_mapping(item) for item in tuple(cfg.get("candidates", ()) or ()))
    configured_from_pdk = bool(configured)
    if not configured:
        configured = (
            {
                "name": f"{logical}_current_native",
                "layout_width_um": float(current_width_um),
                "layout_height_um": float(current_height_um),
                "cost": 0,
                "notes": "current estimated native PCell bbox",
            },
        )
    require_calibrated = bool(cfg.get("require_calibrated", False))
    allow_nearest_calibration = bool(cfg.get("allow_nearest_calibration", False))
    allow_uncalibrated_configured = bool(cfg.get("allow_uncalibrated_configured", True))

    result = []
    seen: set[tuple[tuple[tuple[str, str], ...], int, int]] = set()
    for index, item in enumerate(configured):
        pcell_overrides = _pcell_overrides_from_realization_config(item)
        sizing_overrides = dict(_mapping(item.get("sizing_overrides", {})))
        if not _passive_realization_preserves_electrical_value(
            logical,
            row,
            sizing_overrides,
            cfg,
        ):
            continue
        candidate_row = {**dict(row), **sizing_overrides}
        if pcell_overrides:
            existing_pcell = dict(_mapping(candidate_row.get("pcell_overrides", {})))
            candidate_row["pcell_overrides"] = {**existing_pcell, **pcell_overrides}
        configured_param_overrides = bool(pcell_overrides) or _has_pcell_param_sizing_override(logical, sizing_overrides)
        exported_clean_realization = bool(item.get("calibrated_pcell_realization", False)) or bool(
            item.get("pcell_calibre_usable_for_layout", False)
        )
        width_um, height_um = _configured_candidate_bbox_um(item)
        if width_um <= 0.0 or height_um <= 0.0:
            width_um, height_um = float(current_width_um), float(current_height_um)
        calibrated = _calibrated_bbox_um_for_sizing(
            graph,
            str(device_name),
            candidate_row,
            pdk,
            calibration_cache,
            allow_nearest=allow_nearest_calibration,
        )
        source = "configured_native" if pcell_overrides else "estimated_current_native"
        drc_clean = True
        lvs_clean = True
        if calibrated is not None:
            width_um, height_um, match_policy, clean = calibrated
            source = f"calibration_{match_policy}"
            drc_clean = bool(clean)
            lvs_clean = bool(clean)
        elif exported_clean_realization:
            source = "exported_calibre_catalog"
            drc_clean = True
            lvs_clean = True
        elif require_calibrated:
            continue
        calibre_status = _calibre_catalog_status_for_sizing(
            graph,
            str(device_name),
            candidate_row,
            pdk,
            pcell_calibre_catalog,
        )
        if calibre_status is not None:
            status, usable = calibre_status
            drc_clean = bool(drc_clean and usable)
            lvs_clean = bool(lvs_clean and usable)
        else:
            status, usable = "", True
        layout_aspect_candidate = _candidate_changes_aspect(width_um, height_um, current_width_um, current_height_um)
        if (
            calibrated is None
            and calibre_status is None
            and not exported_clean_realization
            and configured_param_overrides
            and layout_aspect_candidate
            and not allow_uncalibrated_configured
        ):
            continue
        if calibrated is None and not configured_param_overrides and layout_aspect_candidate:
            # A native passive aspect change without native PCell params or an
            # exact calibration entry is just an estimated bbox mutation.  Keep
            # the current native candidate, but do not let it masquerade as a
            # real aspect-ratio realization.
            continue
        key = (_params_signature_tuple(pcell_overrides), round(float(width_um) * 1000), round(float(height_um) * 1000))
        if key in seen:
            continue
        seen.add(key)
        sizing_overrides.update(
            {
                "layout_width_um": float(width_um),
                "layout_height_um": float(height_um),
                "layout_aspect_candidate": bool(layout_aspect_candidate),
                "use_drawn_primitive": False,
                "allow_pcell_aspect_candidates": bool(configured_from_pdk),
                "native_pcell_realization": True,
                "pcell_realization_kind": f"{logical}_native",
                "pcell_realization_source": source,
                "calibrated_pcell_realization": source.startswith("calibration_") or exported_clean_realization,
                "configured_pcell_params": bool(configured_param_overrides),
                "pcell_calibre_status": status,
                "pcell_calibre_usable_for_layout": bool(usable),
            }
        )
        candidate_name = str(item.get("name", f"{logical}_native_{index}"))
        result.append(
            pcell_candidate(
                candidate_name,
                float(width_um),
                float(height_um),
                sizing_overrides=sizing_overrides,
                pcell_overrides=pcell_overrides,
                cost=int(item.get("cost", index) or index),
                drc_clean=drc_clean,
                lvs_clean=lvs_clean,
                notes=str(item.get("notes", f"{logical} native PCell realization from PDK metadata/calibration")),
                metadata=_pcell_candidate_metadata(
                    logical_name=logical,
                    candidate_name=candidate_name,
                    width_um=float(width_um),
                    height_um=float(height_um),
                    sizing_overrides=sizing_overrides,
                    pcell_overrides=pcell_overrides,
                    realization_kind=f"{logical}_native",
                    source=source,
                    clean=bool(drc_clean and lvs_clean),
                ),
            )
        )
    requested_array_m = _passive_unit_array_multiplicity(row)
    if requested_array_m > 1 and pdk is not None:
        try:
            from analogskills.pcell.unit_library import passive_unit_array_candidates_for_m

            for unit_candidate in passive_unit_array_candidates_for_m(pdk, logical, requested_array_m, clean_only=True):
                smt_candidate = unit_candidate.to_smt_candidate_spec()
                key = (
                    _params_signature_tuple(_mapping(getattr(smt_candidate, "pcell_overrides", {}) or {})),
                    round(float(getattr(smt_candidate, "width_um", 0.0)) * 1000),
                    round(float(getattr(smt_candidate, "height_um", 0.0)) * 1000),
                )
                if key in seen:
                    continue
                seen.add(key)
                result.append(smt_candidate)
        except Exception:
            pass
    if not result and configured_from_pdk and not require_calibrated:
        current_candidate = _current_electrical_passive_realization_candidate(
            logical,
            row,
            current_width_um,
            current_height_um,
        )
        if current_candidate is not None:
            result.append(current_candidate)
            result.extend(
                _uncalibrated_passive_aspect_preview_candidates(
                    logical,
                    row,
                    current_width_um,
                    current_height_um,
                )
            )
    return tuple(result)


def _current_electrical_passive_realization_candidate(
    logical: str,
    row: Mapping[str, object],
    current_width_um: float,
    current_height_um: float,
) -> object | None:
    """Return an electrical-value-preserving passive fallback candidate.

    Calibrated CRN28 passive candidates are sparse.  Picking a clean but
    different R/C value makes the placement solver optimize the wrong circuit.
    This fallback keeps the frontend electrical value and exposes the current
    bbox estimate to SMT.  It is intentionally marked in metadata as an
    uncalibrated signoff risk so later PCell calibration can replace it.
    """

    from analogskills.layout.analog_layout_dsl import pcell_candidate

    try:
        width_um = max(float(current_width_um), 1e-6)
        height_um = max(float(current_height_um), 1e-6)
    except (TypeError, ValueError):
        return None
    sizing_overrides = {
        "layout_width_um": width_um,
        "layout_height_um": height_um,
        "layout_aspect_candidate": False,
        "use_drawn_primitive": bool(row.get("use_drawn_primitive", row.get("use_drawn_passive_primitive", False))),
        "allow_pcell_aspect_candidates": False,
        "native_pcell_realization": False,
        "pcell_realization_kind": f"{logical}_current_electrical_bbox",
        "pcell_realization_source": "electrical_preserving_current_bbox",
        "calibrated_pcell_realization": False,
        "configured_pcell_params": False,
        "pcell_calibre_status": "not_calibrated_for_requested_electrical_value",
        "pcell_calibre_usable_for_layout": False,
        "signoff_risk": "requires passive PCell calibration/array realization for requested electrical value",
    }
    metadata = _pcell_candidate_metadata(
        logical_name=logical,
        candidate_name=f"{logical}_current_electrical_bbox",
        width_um=width_um,
        height_um=height_um,
        sizing_overrides=sizing_overrides,
        pcell_overrides={},
        realization_kind=f"{logical}_current_electrical_bbox",
        source="electrical_preserving_current_bbox",
        clean=False,
    )
    metadata["electrical_value_preserved"] = True
    metadata["signoff_risk"] = sizing_overrides["signoff_risk"]
    return pcell_candidate(
        f"{logical}_current_electrical_bbox",
        width_um,
        height_um,
        sizing_overrides=sizing_overrides,
        pcell_overrides={},
        cost=25,
        drc_clean=True,
        lvs_clean=True,
        notes="Electrical-value-preserving passive fallback; use for placement exploration until calibrated R/C PCell array exists.",
        metadata=metadata,
    )


def _uncalibrated_passive_aspect_preview_candidates(
    logical: str,
    row: Mapping[str, object],
    current_width_um: float,
    current_height_um: float,
) -> tuple[object, ...]:
    """Return equal-area passive aspect alternatives for layout-only studies.

    These candidates never claim a native PCell parameterization.  They expose
    the missing realization degree of freedom to the main SMT so observation
    loops can determine whether passive aspect is the actual packing blocker.
    A selected candidate remains explicitly preview-only until replaced by a
    calibrated PCell or unit array.
    """

    if not _bool_like(row.get("allow_uncalibrated_layout_aspect_preview", False)):
        return ()
    from analogskills.layout.analog_layout_dsl import pcell_candidate

    raw_ratios = row.get("layout_aspect_preview_ratios", (2.0, 4.0))
    try:
        ratios = tuple(float(value) for value in tuple(raw_ratios))
    except (TypeError, ValueError):
        ratios = (2.0, 4.0)
    area_um2 = max(float(current_width_um), 1e-6) * max(float(current_height_um), 1e-6)
    current_ratio = max(float(current_width_um), 1e-6) / max(float(current_height_um), 1e-6)
    result: list[object] = []
    for index, ratio in enumerate(ratios):
        if ratio <= 0.0 or abs(ratio - current_ratio) <= 1e-6:
            continue
        width_um = sqrt(area_um2 * ratio)
        height_um = sqrt(area_um2 / ratio)
        candidate_name = f"{logical}_electrical_bbox_preview_ar{ratio:g}"
        signoff_risk = (
            "equal-area planning aspect has no calibrated native PCell/array; "
            "preview only"
        )
        sizing_overrides = {
            "layout_width_um": width_um,
            "layout_height_um": height_um,
            "layout_aspect_candidate": True,
            "layout_aspect_ratio": ratio,
            "use_drawn_primitive": bool(
                row.get("use_drawn_primitive", row.get("use_drawn_passive_primitive", False))
            ),
            "allow_pcell_aspect_candidates": True,
            "native_pcell_realization": False,
            "pcell_realization_kind": f"{logical}_electrical_bbox_preview",
            "pcell_realization_source": "equal_area_uncalibrated_layout_preview",
            "calibrated_pcell_realization": False,
            "configured_pcell_params": False,
            "pcell_calibre_status": "preview_only_not_calibrated",
            "pcell_calibre_usable_for_layout": False,
            "signoff_risk": signoff_risk,
        }
        metadata = _pcell_candidate_metadata(
            logical_name=logical,
            candidate_name=candidate_name,
            width_um=width_um,
            height_um=height_um,
            sizing_overrides=sizing_overrides,
            pcell_overrides={},
            realization_kind=f"{logical}_electrical_bbox_preview",
            source="equal_area_uncalibrated_layout_preview",
            clean=False,
        )
        metadata.update(
            {
                "electrical_value_preserved": True,
                "equal_area_preserved": True,
                "preview_only": True,
                "signoff_risk": signoff_risk,
            }
        )
        result.append(
            pcell_candidate(
                candidate_name,
                width_um,
                height_um,
                sizing_overrides=sizing_overrides,
                pcell_overrides={},
                cost=index + 1,
                drc_clean=True,
                lvs_clean=True,
                notes=(
                    "Equal-area passive aspect preview for global packing; "
                    "requires calibrated PCell/array before physical use."
                ),
                metadata=metadata,
            )
        )
    return tuple(result)


def _passive_realization_preserves_electrical_value(
    logical: str,
    base_row: Mapping[str, object],
    sizing_overrides: Mapping[str, object],
    cfg: Mapping[str, object],
) -> bool:
    """Return False when a passive candidate would silently change R/C."""

    if _bool_like(base_row.get("allow_passive_electrical_override", False)) or _bool_like(
        cfg.get("allow_electrical_override", False)
    ):
        return True
    key_candidates = ("R", "r") if str(logical).lower() == "resistor" else ("C", "c") if str(logical).lower() == "capacitor" else ()
    if not key_candidates:
        return True
    base_value = _first_numeric_value(base_row, key_candidates)
    override_value = _first_numeric_value(sizing_overrides, key_candidates)
    if base_value is None or override_value is None:
        return True
    rel_tol = _positive_float(cfg.get("electrical_value_relative_tolerance", 1e-6), 1e-6)
    abs_tol = _positive_float(cfg.get("electrical_value_absolute_tolerance", 0.0), 0.0)
    return abs(float(base_value) - float(override_value)) <= max(abs(float(base_value)) * rel_tol, abs_tol)


def _first_numeric_value(row: Mapping[str, object], keys: Sequence[str]) -> float | None:
    for key in keys:
        if key not in row:
            continue
        try:
            return float(row[key])
        except (TypeError, ValueError):
            return None
    return None


def _pcell_realization_device_config(pdk: object | None, logical: str) -> Mapping[str, object]:
    cfg = _mapping(_metadata(pdk).get("pcell_realization", {}))
    logical_key = str(logical).lower()
    if logical_key in cfg:
        return _mapping(cfg.get(logical_key, {}))
    if logical_key in {"nmos", "pmos"}:
        mos = dict(_mapping(cfg.get("mos", {})))
        mos.update(dict(_mapping(mos.get(logical_key, {}))))
        return mos
    passives = _mapping(cfg.get("passives", {}))
    if logical_key in passives:
        return _mapping(passives.get(logical_key, {}))
    return {}


def _pcell_overrides_from_realization_config(item: Mapping[str, object]) -> dict[str, object]:
    for key in ("pcell_overrides", "pcell_params", "params"):
        value = _mapping(item.get(key, {}))
        if value:
            return {str(param): raw for param, raw in dict(value).items()}
    return {}


def _has_pcell_param_sizing_override(logical: str, sizing_overrides: Mapping[str, object]) -> bool:
    keys = {str(key) for key in dict(sizing_overrides)}
    logical_key = str(logical).lower()
    if logical_key == "resistor":
        return bool(keys & {"R", "r", "W", "w", "L", "l", "width", "length"})
    if logical_key == "capacitor":
        return bool(keys & {"C", "c", "W", "w", "L", "l", "width", "length"})
    if logical_key == "bjt":
        return bool(keys & {"M", "m"})
    if logical_key in {"nmos", "pmos"}:
        return bool(keys & {"W", "w", "L", "l", "nf", "m", "fingers", "Wfg"})
    return False


def _passive_unit_array_multiplicity(row: Mapping[str, object]) -> int:
    for key in ("passive_unit_array_m", "unit_array_m", "M", "m", "multi"):
        if key not in row:
            continue
        try:
            value = max(1, int(float(row.get(key, 1) or 1)))
        except (TypeError, ValueError):
            continue
        if value > 1:
            return value
    return 1


def _configured_candidate_bbox_um(item: Mapping[str, object]) -> tuple[float, float]:
    bbox = item.get("bbox_um", item.get("bbox"))
    if isinstance(bbox, (tuple, list)) and len(bbox) >= 4:
        try:
            return (abs(float(bbox[2]) - float(bbox[0])), abs(float(bbox[3]) - float(bbox[1])))
        except (TypeError, ValueError):
            return (0.0, 0.0)
    width = _configured_dimension_um(item, ("layout_width_um", "width_um", "bbox_width_um", "w_um"))
    height = _configured_dimension_um(item, ("layout_height_um", "height_um", "bbox_height_um", "h_um"))
    return (width, height)


def _configured_dimension_um(item: Mapping[str, object], keys: Sequence[str]) -> float:
    for key in keys:
        if key in item:
            try:
                return float(item[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _candidate_changes_aspect(width_um: float, height_um: float, ref_width_um: float, ref_height_um: float) -> bool:
    return (
        abs(float(width_um) - float(ref_width_um)) > max(abs(float(ref_width_um)) * 1e-6, 1e-6)
        or abs(float(height_um) - float(ref_height_um)) > max(abs(float(ref_height_um)) * 1e-6, 1e-6)
    )


def _pcell_candidate_metadata(
    *,
    logical_name: str,
    candidate_name: str,
    width_um: float,
    height_um: float,
    sizing_overrides: Mapping[str, object],
    pcell_overrides: Mapping[str, object] | None = None,
    realization_kind: str = "",
    source: str = "",
    clean: bool = True,
) -> dict[str, object]:
    width = max(float(width_um), 1e-9)
    height = max(float(height_um), 1e-9)
    area = width * height
    long_side = max(width, height)
    short_side = max(min(width, height), 1e-9)
    aspect = long_side / short_side
    shape_class = _pcell_shape_class(width, height)
    overrides = _mapping(sizing_overrides)
    array = _mapping(overrides.get("mos_unit_array", overrides.get("bjt_unit_array", overrides.get("passive_unit_array", {}))))
    unit_count = _positive_int(array.get("unit_count", overrides.get("M", overrides.get("m", 1))), 1)
    rows = _positive_int(array.get("rows", 1), 1)
    cols = _positive_int(array.get("cols", 1), 1)
    is_array = bool(array) or "array" in str(realization_kind)
    aspect_cost = max(0, int(round((aspect - 1.0) * 4.0)))
    topology_cost = 0 if shape_class in {"square", "compact"} else 2
    regularity_cost = abs(rows - cols) if is_array and unit_count > 1 else 0
    array_cost = max(0, unit_count - 1) if is_array else 0
    route_access_cost = max(0, min(rows, cols) - 1) if is_array else 0
    fragmentation_cost = max(0, unit_count - 4) if is_array else 0
    pin_access_cost = 0 if bool(clean) else 50
    return {
        "logical_name": str(logical_name),
        "candidate_name": str(candidate_name),
        "realization_kind": str(realization_kind or overrides.get("pcell_realization_kind", "")),
        "realization_source": str(source or overrides.get("pcell_realization_source", "")),
        "width_um": width,
        "height_um": height,
        "area_um2": area,
        "aspect_ratio": aspect,
        "shape_class": shape_class,
        "topology": f"unit_array_{rows}x{cols}" if is_array else "native",
        "unit_count": unit_count,
        "rows": rows,
        "cols": cols,
        "has_pcell_params": bool(_mapping(pcell_overrides or {})),
        "calibre_clean": bool(clean),
        "access_contract": _pcell_candidate_access_contract(width, height, overrides),
        "shape_cost": topology_cost + aspect_cost,
        "aspect_cost": aspect_cost,
        "topology_cost": topology_cost,
        "array_cost": array_cost,
        "regularity_cost": regularity_cost,
        "route_access_cost": route_access_cost,
        "fragmentation_cost": fragmentation_cost,
        "pin_access_cost": pin_access_cost,
    }


def _pcell_candidate_access_contract(width_um: float, height_um: float, overrides: Mapping[str, object]) -> dict[str, object]:
    footprint_model = str(
        overrides.get(
            "pcell_realization_footprint",
            overrides.get("array_access_envelope_model", "native_pcell_bbox"),
        )
        or "native_pcell_bbox"
    )
    bbox_x0 = _float_or_default(overrides.get("layout_bbox_x0_um", overrides.get("bbox_x0_um")), 0.0)
    bbox_y0 = _float_or_default(overrides.get("layout_bbox_y0_um", overrides.get("bbox_y0_um")), 0.0)
    unit_bbox_x0 = _float_or_default(overrides.get("unit_layout_bbox_x0_um", overrides.get("unit_bbox_x0_um")), 0.0)
    unit_bbox_y0 = _float_or_default(overrides.get("unit_layout_bbox_y0_um", overrides.get("unit_bbox_y0_um")), 0.0)
    uses_generated_access = "access" in footprint_model.lower() or bool(
        overrides.get("layout_access_envelope_model", False)
    )
    row: dict[str, object] = {
        "footprint_model": footprint_model,
        "placement_bbox_um": [0.0, 0.0, float(width_um), float(height_um)],
        "bbox_origin_um": [bbox_x0, bbox_y0],
        "uses_generated_access_envelope": bool(uses_generated_access),
    }
    array = _mapping(overrides.get("mos_unit_array", overrides.get("bjt_unit_array", overrides.get("passive_unit_array", {}))))
    if array:
        row["unit_bbox_origin_um"] = [unit_bbox_x0, unit_bbox_y0]
        row["array_rows"] = _positive_int(array.get("rows", 1), 1)
        row["array_cols"] = _positive_int(array.get("cols", 1), 1)
        row["array_unit_count"] = _positive_int(array.get("unit_count", 1), 1)
    return row


def _float_or_default(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _pcell_shape_class(width_um: float, height_um: float) -> str:
    width = max(float(width_um), 1e-9)
    height = max(float(height_um), 1e-9)
    ratio = max(width, height) / max(min(width, height), 1e-9)
    if ratio <= 1.15:
        return "square"
    if ratio <= 2.0:
        return "compact"
    return "wide" if width > height else "tall"


def _params_signature_tuple(params: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), repr(value)) for key, value in dict(params).items()))


def _calibrated_bbox_um_for_sizing(
    graph: TopologyGraph,
    device_name: str,
    sizing: Mapping[str, object],
    pdk: object | None,
    calibration_cache: object | None,
    *,
    allow_nearest: bool = False,
) -> tuple[float, float, str, bool] | None:
    if pdk is None or calibration_cache is None:
        return None
    lookup = getattr(calibration_cache, "lookup", None)
    if lookup is None:
        return None
    try:
        from analogskills.pcell.generation import logical_pcell_name, pcell_params_for_device

        device = graph.devices[str(device_name)]
        logical = str(logical_pcell_name(device))
        template = pdk.pcell_template_for(logical)
        params = pcell_params_for_device(device, dict(sizing), template, pdk=pdk)
        entry = lookup(
            logical_name=logical,
            pcell=f"{template.resolved_layout_lib_name()}/{template.resolved_layout_cell_name()}/{template.resolved_layout_view_name()}",
            params=params,
            orient=str(dict(sizing).get("orient", "R0") or "R0"),
            allow_nearest=bool(allow_nearest),
        )
    except Exception:
        return None
    if entry is None:
        return None
    bbox = getattr(entry, "instance_bbox_um", None) or getattr(entry, "bbox_um", None)
    if not isinstance(bbox, (tuple, list)) or len(bbox) < 4:
        return None
    try:
        width_um = abs(float(bbox[2]) - float(bbox[0]))
        height_um = abs(float(bbox[3]) - float(bbox[1]))
    except (TypeError, ValueError):
        return None
    if width_um <= 0.0 or height_um <= 0.0:
        return None
    metadata = _mapping(getattr(entry, "metadata", {}) or {})
    match_policy = str(metadata.get("match_policy", "exact") or "exact")
    clean = not tuple(getattr(entry, "errors", ()) or ())
    return (width_um, height_um, match_policy, bool(clean))


def _mos_generated_access_footprint_bbox_um(
    graph: TopologyGraph,
    device_name: str,
    sizing: Mapping[str, object],
    pdk: object | None,
    *,
    width_um: float,
    height_um: float,
) -> tuple[float, float, float, float] | None:
    if pdk is None or str(getattr(pdk, "name", "")).lower() != "crn28hpcp":
        return None
    try:
        from analogskills.pcell.calibre_calibration import _crn28_mos_multifinger_access_rects_for_instance
        from analogskills.pcell.generation import FingerChoice, PCellInstancePlan, logical_pcell_name, pcell_params_for_device

        device = graph.devices[str(device_name)]
        logical = str(logical_pcell_name(device)).lower()
        if logical not in {"nmos", "pmos"}:
            return None
        template = pdk.pcell_template_for(logical)
        nf = max(1, int(float(sizing.get("nf", sizing.get("fingers", 1)) or 1)))
        mult = max(1, int(float(sizing.get("m", sizing.get("M", sizing.get("simM", 1))) or 1)))
        width_m = _sizing_dimension_m(sizing, ("W", "w", "width"), 1e-6)
        length_m = _sizing_dimension_m(sizing, ("L", "l", "length"), 0.18e-6)
        finger_width_m = float(sizing.get("wf", sizing.get("Wfg", width_m / float(max(nf * mult, 1)))) or (width_m / float(max(nf * mult, 1))))
        finger_choice = FingerChoice(
            nf=int(nf),
            m=int(mult),
            finger_width_m=float(finger_width_m),
            total_width_m=float(width_m),
            length_m=float(length_m),
        )
        params = pcell_params_for_device(device, dict(sizing), template, finger_choice=finger_choice, pdk=pdk)  # type: ignore[arg-type]
        inst = PCellInstancePlan(
            name=str(device_name),
            logical_name=logical,
            lib_name=template.resolved_layout_lib_name(),
            cell_name=template.resolved_layout_cell_name(),
            view_name=template.resolved_layout_view_name(),
            params=dict(params),
            xy_um=(0.0, 0.0),
            orient="R0",
            connections={str(term): str(term) for term in tuple(getattr(device, "terminals", ()) or ())},
            width_um=max(float(width_um), 1e-9),
            height_um=max(float(height_um), 1e-9),
        )
        rects = tuple(_crn28_mos_multifinger_access_rects_for_instance(pdk, inst))  # type: ignore[arg-type]
    except Exception:
        return None
    boxes = [(0.0, 0.0, max(float(width_um), 1e-9), max(float(height_um), 1e-9))]
    for rect in rects:
        bbox = getattr(rect, "bbox", None)
        if isinstance(bbox, (tuple, list)) and len(bbox) >= 4:
            try:
                boxes.append((float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])))
            except (TypeError, ValueError):
                continue
    x0 = min(box[0] for box in boxes)
    y0 = min(box[1] for box in boxes)
    x1 = max(box[2] for box in boxes)
    y1 = max(box[3] for box in boxes)
    try:
        return tuple(getattr(pdk, "rules").snap_bbox_um((x0, y0, x1, y1), mode="outward"))  # type: ignore[return-value]
    except Exception:
        return (x0, y0, x1, y1)


def _mos_access_aware_footprint_enabled(pdk: object | None) -> bool:
    """Opt in to using generated MOS access geometry as the SMT placement footprint.

    This is deliberately disabled by default.  Simply inflating MOS candidates
    by generated body/access metal can change the global packing and create new
    access-to-access shorts unless those access keepouts are also represented in
    the SMT constraints.  Keep the feature available for experiments, but do not
    perturb the clean default flow.
    """
    env = get_env("MOS_ACCESS_AWARE_FOOTPRINT")
    if has_env("MOS_ACCESS_AWARE_FOOTPRINT"):
        return _truthy_env(env)
    metadata = _mapping(getattr(pdk, "metadata", {}) if pdk is not None else {})
    layout_cfg = _mapping(metadata.get("layout", {}))
    mos_cfg = _mapping(layout_cfg.get("mos_access_aware_footprint", {}))
    if "enabled" in mos_cfg:
        return _bool_like(mos_cfg.get("enabled"))
    return False


def _calibre_catalog_status_for_sizing(
    graph: TopologyGraph,
    device_name: str,
    sizing: Mapping[str, object],
    pdk: object | None,
    catalog: object | None,
) -> tuple[str, bool] | None:
    if pdk is None or catalog is None:
        return None
    entries = tuple(getattr(catalog, "entries", ()) or ())
    if not entries:
        return None
    try:
        from analogskills.pcell.generation import logical_pcell_name, pcell_params_for_device

        device = graph.devices[str(device_name)]
        logical = str(logical_pcell_name(device))
        template = pdk.pcell_template_for(logical)
        params = pcell_params_for_device(device, dict(sizing), template, pdk=pdk)
        pcell_key = f"{template.resolved_layout_lib_name()}/{template.resolved_layout_cell_name()}/{template.resolved_layout_view_name()}"
        requested = _params_signature_tuple(params)
    except Exception:
        return None
    for entry in entries:
        target = getattr(entry, "target", None)
        if target is None:
            continue
        if str(getattr(target, "logical_name", "")) != logical:
            continue
        if str(getattr(target, "pcell_key", "")) != pcell_key:
            continue
        if _params_signature_tuple(getattr(target, "params", {}) or {}) != requested:
            continue
        classification = _mapping(getattr(entry, "classification", {}) or {})
        status = str(classification.get("status", "") or "")
        usable = bool(classification.get("usable_for_layout", False)) or bool(getattr(entry, "usable_for_layout", False))
        return (status, usable)
    return None


def _passive_aspect_candidates_are_enabled(
    sizing: Mapping[str, object],
    pdk: object | None,
) -> bool:
    if bool(sizing.get("allow_pcell_aspect_candidates", False)):
        return True
    if bool(sizing.get("use_drawn_primitive", sizing.get("use_drawn_passive_primitive", False))):
        return True
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    metadata = metadata if isinstance(metadata, Mapping) else {}
    layout_cfg = metadata.get("layout", {})
    layout_cfg = layout_cfg if isinstance(layout_cfg, Mapping) else {}
    return bool(layout_cfg.get("allow_native_passive_aspect_candidates", False))


def _passive_candidate_is_terminal_access_safe(
    logical: str,
    sizing: Mapping[str, object],
    pdk: object | None,
    width_um: float,
    height_um: float,
) -> bool:
    """Reject passive aspect candidates that invalidate static terminal anchors.

    For native CRN28 passives the PDK template currently exposes fixed numeric
    terminal-access coordinates.  Until aspect-specific calibration entries are
    available, shrinking/rotating the estimated bbox can put the access point
    outside the device envelope and make routing observations misleading.  Drawn
    primitive passives are geometric and can safely use all bbox variants.
    """

    if bool(sizing.get("use_drawn_primitive", sizing.get("use_drawn_passive_primitive", False))):
        return True
    if pdk is None:
        return True
    try:
        template = pdk.pcell_template_for(str(logical))
    except Exception:
        return True
    extents = _template_terminal_access_extents_um(getattr(template, "terminal_access", {}) or {})
    if extents is None:
        return True
    x0, y0, x1, y1 = extents
    margin = 0.02
    return (x1 - x0) <= float(width_um) + margin and (y1 - y0) <= float(height_um) + margin


def _template_terminal_access_extents_um(access: Mapping[str, object]) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    for row_obj in dict(access).values():
        row = _mapping(row_obj)
        xy = row.get("xy")
        if not isinstance(xy, (tuple, list)) or len(xy) < 2:
            continue
        try:
            points.append((float(xy[0]), float(xy[1])))
        except (TypeError, ValueError):
            return None
    if not points:
        return None
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def _natural_suffix_key(name: str) -> tuple[int, str]:
    text = str(name)
    try:
        return (int(text.rsplit("_", 1)[1]), text)
    except (IndexError, ValueError):
        return (-1 if text == "R1" else 10_000, text)


def _analog_rule_strategy_for_result(
    result: AnalogHierarchicalSmtResult,
    block: str,
    pdk: object | None,
) -> Mapping[str, object]:
    existing = result.problem.rule_metadata.get("rule_strategy", {})
    if isinstance(existing, Mapping) and existing:
        return existing
    return resolve_smt_rule_strategy(pdk, block)


def _apply_configured_layout_relation_overrides(spec: object, pdk: object | None, block: str) -> object:
    """Overlay PDK-configured hard pattern relations before compiling SMT."""

    raw = _block_smt_rules(pdk, block).get("placement_relation_overrides", ())
    if isinstance(raw, Mapping):
        rows = tuple(raw.values())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        rows = tuple(raw)
    else:
        rows = ()
    if not rows or not hasattr(spec, "relations"):
        return spec

    from analogskills.layout.analog_layout_dsl import PatternRelationSpec

    relations = list(tuple(getattr(spec, "relations", ()) or ()))
    changed = False
    for item in rows:
        row = _mapping(item)
        source = str(row.get("source", "") or "").strip()
        target = str(row.get("target", "") or "").strip()
        kind = str(row.get("kind", "") or "").strip()
        if not source or not target or not kind:
            continue
        default_gap = 0.0
        default_tol = 0.0
        match_index = None
        for idx, relation in enumerate(relations):
            if (
                str(getattr(relation, "source", "")) == source
                and str(getattr(relation, "target", "")) == target
                and str(getattr(relation, "kind", "")) == kind
            ):
                match_index = idx
                default_gap = float(getattr(relation, "min_gap_um", 0.0) or 0.0)
                default_tol = float(getattr(relation, "tolerance_um", 0.0) or 0.0)
                break
        min_gap_um = _dimension_cfg_um(row, "min_gap_um", "min_gap_nm", default_gap)
        tolerance_um = _dimension_cfg_um(row, "tolerance_um", "tolerance_nm", default_tol)
        mode = str(row.get("mode", row.get("operation", "max")) or "max").strip().lower()
        notes = str(row.get("notes", "configured_layout_relation_override") or "")
        if match_index is None:
            relations.append(PatternRelationSpec(source, target, kind, min_gap_um, tolerance_um, notes))
            changed = True
            continue
        current = relations[match_index]
        gap = float(min_gap_um)
        tol = float(tolerance_um)
        if mode not in {"set", "override"}:
            gap = max(float(getattr(current, "min_gap_um", 0.0) or 0.0), gap)
            tol = max(float(getattr(current, "tolerance_um", 0.0) or 0.0), tol)
        updated = replace(current, min_gap_um=gap, tolerance_um=tol, notes=notes or getattr(current, "notes", ""))
        if updated != current:
            relations[match_index] = updated
            changed = True
    if not changed:
        return spec
    return replace(spec, relations=tuple(relations))


def _apply_configured_layout_pack_overrides(spec: object, pdk: object | None, block: str) -> object:
    """Overlay PDK-configured local compact-window constraints."""

    raw = _block_smt_rules(pdk, block).get("placement_pack_overrides", ())
    if isinstance(raw, Mapping):
        rows = tuple(raw.values())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        rows = tuple(raw)
    else:
        rows = ()
    if not rows or not hasattr(spec, "pack_constraints"):
        return spec

    from analogskills.layout.analog_layout_dsl import PackConstraintSpec

    packs = list(tuple(getattr(spec, "pack_constraints", ()) or ()))
    by_name = {str(getattr(pack, "name", "")): idx for idx, pack in enumerate(packs)}
    changed = False
    for item in rows:
        row = _mapping(item)
        name = str(row.get("name", "") or "").strip()
        raw_patterns = row.get("patterns", row.get("groups", ()))
        if isinstance(raw_patterns, str):
            patterns = (raw_patterns,)
        elif isinstance(raw_patterns, Sequence):
            patterns = tuple(str(pattern) for pattern in raw_patterns if str(pattern))
        else:
            patterns = ()
        if not name or not patterns:
            continue
        max_width_um = _optional_dimension_cfg_um(row, "max_width_um", "max_width_nm")
        max_height_um = _optional_dimension_cfg_um(row, "max_height_um", "max_height_nm")
        notes = str(row.get("notes", "configured_layout_pack_override") or "")
        new_pack = PackConstraintSpec(
            name=name,
            patterns=tuple(dict.fromkeys(patterns)),
            max_width_um=max_width_um,
            max_height_um=max_height_um,
            weight=max(1, int(row.get("weight", 1) or 1)),
            width_weight=max(0, int(row.get("width_weight", 1) or 1)),
            height_weight=max(0, int(row.get("height_weight", 1) or 1)),
            area_weight=max(0, int(row.get("area_weight", 0) or 0)),
            notes=notes,
        )
        if name in by_name:
            current = packs[by_name[name]]
            mode = str(row.get("mode", row.get("operation", "override")) or "override").strip().lower()
            if mode in {"merge", "max"}:
                merged = replace(
                    current,
                    patterns=tuple(dict.fromkeys(tuple(getattr(current, "patterns", ()) or ()) + new_pack.patterns)),
                    max_width_um=_merge_optional_limit(getattr(current, "max_width_um", None), new_pack.max_width_um, prefer_min=True),
                    max_height_um=_merge_optional_limit(getattr(current, "max_height_um", None), new_pack.max_height_um, prefer_min=True),
                    weight=max(int(getattr(current, "weight", 1) or 1), new_pack.weight),
                    width_weight=max(int(getattr(current, "width_weight", 1) or 1), new_pack.width_weight),
                    height_weight=max(int(getattr(current, "height_weight", 1) or 1), new_pack.height_weight),
                    notes=notes or getattr(current, "notes", ""),
                )
                new_pack = merged
            if new_pack != current:
                packs[by_name[name]] = new_pack
                changed = True
        else:
            by_name[name] = len(packs)
            packs.append(new_pack)
            changed = True
    if not changed:
        return spec
    return replace(spec, pack_constraints=tuple(packs))


def _merge_optional_limit(left: object, right: object, *, prefer_min: bool) -> float | None:
    values = [float(value) for value in (left, right) if value is not None]
    if not values:
        return None
    return min(values) if prefer_min else max(values)


def _optional_dimension_cfg_um(cfg: Mapping[str, object], um_key: str, nm_key: str) -> float | None:
    value = cfg.get(um_key, None)
    if value is None and nm_key in cfg:
        try:
            value = float(cfg[nm_key]) * 1e-3
        except (TypeError, ValueError):
            return None
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return None


def _flat_corridor_bboxes_um(
    corridors: Sequence[HierarchicalRoutingCorridor2D],
    group_bboxes_tracks: Mapping[str, tuple[int, int, int, int]],
    capacities: Mapping[str, int],
    track_pitch_um: float,
) -> dict[str, tuple[float, float, float, float]]:
    result: dict[str, tuple[float, float, float, float]] = {}
    for corridor in corridors:
        source = group_bboxes_tracks.get(corridor.source_group)
        target = group_bboxes_tracks.get(corridor.target_group)
        if source is None or target is None:
            continue
        cap = max(1, int(capacities.get(corridor.name, corridor.base_capacity_tracks or 1)))
        result[corridor.name] = _bbox_tracks_to_um(
            _flat_corridor_bbox_tracks(source, target, corridor.orientation, cap),
            track_pitch_um,
        )
    return result


def _flat_corridor_bbox_tracks(
    source: tuple[int, int, int, int],
    target: tuple[int, int, int, int],
    orientation: str,
    capacity_tracks: int,
) -> tuple[int, int, int, int]:
    sx0, sy0, sx1, sy1 = source
    tx0, ty0, tx1, ty1 = target
    cap = max(1, int(capacity_tracks))
    if orientation == "horizontal":
        if sx1 <= tx0:
            x0, x1 = sx1, tx0
        elif tx1 <= sx0:
            x0, x1 = tx1, sx0
        else:
            x0, x1 = max(sx0, tx0), min(sx1, tx1)
        if x1 <= x0:
            mid_x = (_bbox_center_tracks(source)[0] + _bbox_center_tracks(target)[0]) // 2
            x0, x1 = mid_x, mid_x + cap
        y0, y1 = max(sy0, ty0), min(sy1, ty1)
        if y1 <= y0:
            mid_y = (_bbox_center_tracks(source)[1] + _bbox_center_tracks(target)[1]) // 2
            y0, y1 = mid_y, mid_y + cap
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    if sy1 <= ty0:
        y0, y1 = sy1, ty0
    elif ty1 <= sy0:
        y0, y1 = ty1, sy0
    else:
        y0, y1 = max(sy0, ty0), min(sy1, ty1)
    if y1 <= y0:
        mid_y = (_bbox_center_tracks(source)[1] + _bbox_center_tracks(target)[1]) // 2
        y0, y1 = mid_y, mid_y + cap
    x0, x1 = max(sx0, tx0), min(sx1, tx1)
    if x1 <= x0:
        mid_x = (_bbox_center_tracks(source)[0] + _bbox_center_tracks(target)[0]) // 2
        x0, x1 = mid_x, mid_x + cap
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _bbox_center_tracks(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
    return ((bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2)


def optimize_crn28_mos_sizing_for_drc(
    graph: TopologyGraph,
    sizing: Mapping[str, Mapping[str, object]],
    pdk: object,
) -> dict[str, dict[str, object]]:
    """Return a CRN28 MOS sizing map with DRC-safe nf and native PCell knobs.

    This is intentionally explicit at the sizing artifact boundary: schematic,
    layout PCells, and LVS source must all see the same nf/m choices.  The
    design-rule numbers and PCell CDF parameters are read from the PDK config
    instead of being embedded in the generator.
    """

    result: dict[str, dict[str, object]] = {str(name): dict(params) for name, params in dict(sizing).items()}
    if str(getattr(pdk, "name", "")).lower() != "crn28hpcp":
        return result
    rules = _crn28_mos_finger_rule_config(pdk)
    if not bool(rules.get("enabled", False)):
        return result
    max_by_logical = _mapping(rules.get("max_finger_width_nm_by_logical", {}))
    min_finger_nm = float(rules.get("min_finger_width_nm", 0.0) or 0.0)
    max_nf = max(1, int(rules.get("max_nf", 128) or 128))
    max_m = max(1, int(rules.get("max_m", 64) or 64))
    prefer_even_nf = bool(rules.get("prefer_even_nf", True))
    pcell_overrides = _crn28_mos_pcell_overrides(pdk, str(rules.get("variant", "") or ""))

    for name, device in graph.devices.items():
        logical = _mos_logical_name_for_device(device)
        if logical not in {"nmos", "pmos"}:
            continue
        row = result.setdefault(str(name), {})
        if pcell_overrides.get(logical):
            existing = dict(_mapping(row.get("pcell_overrides", {})))
            row["pcell_overrides"] = {**dict(pcell_overrides[logical]), **existing}
        max_finger_nm = float(max_by_logical.get(logical, max_by_logical.get("default", 0.0)) or 0.0)
        if max_finger_nm <= 0.0:
            continue
        width_m = _sizing_dimension_m(row, ("W", "w", "width"), 1e-6)
        length_m = _sizing_dimension_m(row, ("L", "l", "length"), 0.18e-6)
        if width_m <= 0.0 or length_m <= 0.0:
            continue
        target_units = max(1, int(ceil(width_m / (max_finger_nm * 1e-9))))
        if prefer_even_nf and target_units > 1 and target_units % 2:
            target_units += 1
        current_nf = max(1, int(row.get("nf", 1) or 1))
        current_m = max(1, int(row.get("m", 1) or 1))
        current_wfg_nm = width_m / max(current_nf * current_m, 1) * 1e9
        if _mos_finger_choice_respects_config(
            current_nf,
            current_m,
            current_wfg_nm * 1e-9,
            min_finger_m=min_finger_nm * 1e-9 if min_finger_nm > 0.0 else 1e-12,
            max_finger_m=max_finger_nm * 1e-9,
            max_nf=max_nf,
            max_m=max_m,
        ):
            continue
        nf = min(max_nf, target_units)
        if prefer_even_nf and nf > 1 and nf % 2 and nf < max_nf:
            nf += 1
        m = max(1, int(ceil(target_units / max(nf, 1))))
        if m > max_m:
            m = max_m
            nf = min(max_nf, max(1, int(ceil(target_units / max_m))))
            if prefer_even_nf and nf > 1 and nf % 2 and nf < max_nf:
                nf += 1
        if min_finger_nm > 0.0:
            while nf > 1 and width_m / max(nf * m, 1) * 1e9 < min_finger_nm:
                nf -= 1
        row["nf"] = int(max(1, nf))
        row["m"] = int(max(1, m))
        row["mos_dynamic_generation_policy"] = "config_constrained"
    return result


def _bandgap_resistor_ladder_mid_nets(graph: TopologyGraph) -> tuple[str, ...]:
    def sort_key(name: str) -> tuple[int, str]:
        try:
            return (int(str(name).rsplit("_", 1)[1]), str(name))
        except (IndexError, ValueError):
            return (10_000, str(name))

    return tuple(sorted((str(net) for net in graph.nets if str(net).startswith("r2_mid_")), key=sort_key))


def _build_structured_interconnect_plan(
    graph: TopologyGraph,
    pcell_plan: object,
    pdk: object,
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
    route_specs: Sequence[tuple[str, Sequence[str], str, float, int, str]],
    *,
    lib: str,
    cell: str,
    calibration_cache: object | None = None,
    fixed_obstacle_plan: object | None = None,
) -> AnalogStructuredRouteResult:
    from analogskills.eda.oa import OaCellView, OaPath, OaRect, OaWritePlan, snap_oa_write_plan_to_grid

    route_specs, route_resource_overrides = _apply_dsl_route_resources_to_route_specs(route_specs, pdk, smt_result)
    anchors_by_net = _collect_terminal_anchors_by_net(
        graph,
        pcell_plan,
        pdk,
        tuple(spec[0] for spec in route_specs),
        calibration_cache=calibration_cache,
    )
    routing_origin = _structured_routing_origin(smt_result)
    corridor_boxes_um = _structured_corridor_boxes_um(smt_result)
    paths: list[object] = []
    rects: list[object] = []
    route_rows: list[dict[str, object]] = []
    for net, corridor_names, layer, width, lane, demand_name in route_specs:
        legal_width = _legal_route_width_um(pdk, layer, float(width))
        raw_anchors = tuple(anchors_by_net.get(net, ()))
        local_bus_rects, anchors, anchor_compaction = _compact_structured_unit_array_route_anchors(
            net,
            raw_anchors,
            pdk,
            route_layer=layer,
            route_width_um=legal_width,
            rect_factory=OaRect,
        )
        boxes = tuple(corridor_boxes_um[name] for name in corridor_names if name in corridor_boxes_um)
        net_rects = _structured_access_rects_for_net(
            net,
            anchors,
            pdk,
            route_layer=layer,
            route_width_um=legal_width,
            rect_factory=OaRect,
        )
        net_rects = (*local_bus_rects, *net_rects)
        net_paths = _structured_paths_for_net(
            net,
            anchors,
            boxes,
            pdk=pdk,
            layer=layer,
            width=legal_width,
            lane=int(lane),
            route_policy=route_resource_overrides.get(net),
            path_factory=OaPath,
        )
        rects.extend(net_rects)
        paths.extend(net_paths)
        route_rows.append(
            {
                "net": net,
                "demand": demand_name,
                "corridors": tuple(corridor_names),
                "layer": layer,
                "width_um": legal_width,
                "requested_width_um": float(width),
                "lane": int(lane),
                "raw_anchor_count": len(raw_anchors),
                "anchor_count": len(anchors),
                "local_bus_rect_count": len(local_bus_rects),
                "unit_array_anchor_compaction": anchor_compaction,
                "access_rect_count": len(net_rects),
                "path_count": len(net_paths),
                "routing_origin": routing_origin,
                "route_resource": route_resource_overrides.get(net),
            }
        )
    plan = OaWritePlan(
        OaCellView(lib, cell, "layout", "maskLayout"),
        nets=tuple(dict.fromkeys(spec[0] for spec in route_specs)),
        rects=tuple(rects),
        paths=tuple(paths),
    )
    plan = snap_oa_write_plan_to_grid(plan, pdk)
    fixed_obstacle_summary: dict[str, object] = {
        "enabled": fixed_obstacle_plan is not None,
        "rect_count": 0,
        "via_count": 0,
        "path_count": 0,
        "routing_contract": "foreign-net paths must not cross fixed physical obstacles",
    }
    if fixed_obstacle_plan is None:
        plan, local_short_jog_repair = _repair_structured_path_rect_shorts(plan, pdk)
    else:
        # The obstacle geometry participates in physical short analysis while
        # only generated route paths are mutable.  This makes a guard ring (or
        # another fixed macro boundary) visible during route construction,
        # rather than discovering the collision only after final merge.
        obstacle_rects = tuple(getattr(fixed_obstacle_plan, "rects", ()) or ())
        obstacle_vias = tuple(getattr(fixed_obstacle_plan, "vias", ()) or ())
        obstacle_paths = tuple(getattr(fixed_obstacle_plan, "paths", ()) or ())
        fixed_obstacle_summary.update(
            {
                "rect_count": len(obstacle_rects),
                "via_count": len(obstacle_vias),
                "path_count": len(obstacle_paths),
            }
        )
        repair_context = OaWritePlan(
            plan.cellview,
            nets=tuple(dict.fromkeys((*tuple(getattr(fixed_obstacle_plan, "nets", ()) or ()), *plan.nets))),
            rects=(*obstacle_rects, *plan.rects),
            paths=(*obstacle_paths, *plan.paths),
            vias=(*obstacle_vias, *plan.vias),
        )
        initial_obstacle_shorts = _fixed_obstacle_cross_net_shorts(
            repair_context,
            pdk,
            rect_count=len(obstacle_rects),
            path_count=len(obstacle_paths),
            via_count=len(obstacle_vias),
        )
        repaired_context, local_short_jog_repair = _repair_structured_path_rect_shorts(repair_context, pdk)
        fixed_path_count = len(obstacle_paths)
        repaired_route_paths = tuple(getattr(repaired_context, "paths", ()) or ())[fixed_path_count:]
        plan = replace(plan, paths=repaired_route_paths)
        final_obstacle_shorts = _fixed_obstacle_cross_net_shorts(
            repaired_context,
            pdk,
            rect_count=len(obstacle_rects),
            path_count=len(obstacle_paths),
            via_count=len(obstacle_vias),
        )
        fixed_obstacle_summary.update(
            {
                "initial_cross_net_short_count": len(initial_obstacle_shorts),
                "final_cross_net_short_count": len(final_obstacle_shorts),
                "final_cross_net_shorts": final_obstacle_shorts,
                "initial_total_route_short_count": int(local_short_jog_repair.get("initial_short_count", 0) or 0),
                "final_total_route_short_count": int(local_short_jog_repair.get("final_short_count", 0) or 0),
                "repair_edit_count": len(tuple(local_short_jog_repair.get("edits", ()) or ())),
                "passed": not final_obstacle_shorts,
            }
        )
    rule_strategy = _structured_rule_strategy(smt_result)
    summary = {
        "routing_origin": routing_origin,
        "block": smt_result.block,
        "route_count": len(route_rows),
        "path_count": len(paths),
        "routes": tuple(route_rows),
        "dsl_route_resource_overrides": tuple(route_resource_overrides.values()),
        "smt_corridor_capacity_tracks": _structured_corridor_capacity_tracks(smt_result),
        "smt_critical_load_by_corridor": _structured_critical_load_by_corridor(smt_result),
        "smt_mode": str(rule_strategy.get("mode", "hybrid")),
        "rule_family_owners": dict(_mapping(rule_strategy.get("rule_family_owners", {}))),
        "rule_owner_schema_version": rule_strategy.get("owner_schema_version"),
        "main_smt_hard_rule_families": tuple(rule_strategy.get("main_smt_hard_rule_families", ()) or ()),
        "main_smt_proxy_rule_families": tuple(rule_strategy.get("main_smt_proxy_rule_families", ()) or ()),
        "main_smt_rule_families": tuple(rule_strategy.get("main_smt_rule_families", ()) or ()),
        "local_smt_rule_families": tuple(rule_strategy.get("local_smt_rule_families", ()) or ()),
        "a_star_rule_families": tuple(rule_strategy.get("a_star_rule_families", ()) or ()),
        "eco_rule_families": tuple(rule_strategy.get("eco_rule_families", ()) or ()),
        "signoff_only_rule_families": tuple(rule_strategy.get("signoff_only_rule_families", ()) or ()),
        "external_eco_rule_families": tuple(rule_strategy.get("external_eco_rule_families", ()) or ()),
        "local_short_jog_repair": local_short_jog_repair,
        "fixed_obstacles": fixed_obstacle_summary,
    }
    return AnalogStructuredRouteResult(plan, summary)


def _fixed_obstacle_cross_net_shorts(
    plan: object,
    pdk: object,
    *,
    rect_count: int,
    path_count: int,
    via_count: int,
) -> tuple[Mapping[str, object], ...]:
    """Return only physical shorts that touch the fixed prefix of a route plan."""

    from analogskills.layout.physical import analyze_plan_physical_connectivity

    try:
        report = analyze_plan_physical_connectivity(plan, pdk=pdk)
    except Exception:
        return ()
    prefixes = tuple(
        [f"rect[{index}]" for index in range(max(0, int(rect_count)))]
        + [f"path[{index}]" for index in range(max(0, int(path_count)))]
        + [f"via[{index}]" for index in range(max(0, int(via_count)))]
    )

    def touches_fixed(source: object) -> bool:
        text = str(source or "")
        return any(text == prefix or text.startswith(f"{prefix}.") for prefix in prefixes)

    return tuple(
        dict(row)
        for row in tuple(report.get("shorts", ()) or ())
        if touches_fixed(row.get("source_a", "")) or touches_fixed(row.get("source_b", ""))
    )


def _repair_structured_path_rect_shorts(
    plan: object,
    pdk: object,
    *,
    max_iterations: int = 8,
    max_path_candidates_per_iteration: int = 1,
    max_candidate_evaluations_per_iteration: int = 64,
    max_seconds: float | None = None,
) -> tuple[object, Mapping[str, object]]:
    """Jog single-segment structured route trunks away from same-layer landing shorts.

    This is intentionally a narrow ECO primitive.  It only moves a path when the
    inline physical precheck proves that a same-layer short is between that path
    and one or more small fixed rect landings.  Candidate jogs are accepted only
    if the total short count decreases.
    """

    from analogskills.eda.oa import snap_oa_write_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity

    if max_seconds is None:
        try:
            max_seconds = float(get_env("STRUCTURED_SHORT_REPAIR_MAX_SECONDS", "20") or "20")
        except ValueError:
            max_seconds = 20.0
    deadline = time.monotonic() + max_seconds if max_seconds and max_seconds > 0.0 else None
    budget_exhausted = False

    try:
        report = analyze_plan_physical_connectivity(plan, pdk=pdk)
    except Exception:
        return plan, {"enabled": False, "reason": "physical_precheck_unavailable"}
    initial_shorts = tuple(report.get("shorts", ()) or ())
    if not initial_shorts:
        return plan, {"enabled": True, "initial_short_count": 0, "final_short_count": 0, "edits": ()}

    current_plan = plan
    current_report = report
    current_count = len(initial_shorts)
    edits: list[dict[str, object]] = []
    evaluation_count = 0
    repair_policy = _structured_short_jog_repair_policy(pdk)
    min_repair_width_um = max(
        _dimension_cfg_um(repair_policy, "min_path_width_um", "min_path_width_nm", 0.0),
        0.0,
    )
    max_repair_jog_offset_um = max(
        _dimension_cfg_um(repair_policy, "max_jog_offset_um", "max_jog_offset_nm", 2.0),
        0.0,
    )
    for _iteration in range(max(0, int(max_iterations))):
        if deadline is not None and time.monotonic() >= deadline:
            budget_exhausted = True
            break
        shorts = tuple(current_report.get("shorts", ()) or ())
        if not shorts:
            break
        paths = list(tuple(getattr(current_plan, "paths", ()) or ()))
        best_plan = current_plan
        best_report = current_report
        best_count = current_count
        best_path = None
        best_path_idx = None
        best_edit: dict[str, object] | None = None
        best_change_found = False
        iteration_evaluations = 0
        path_indices = _path_indices_by_rect_short_count(shorts)
        for path_idx in path_indices[: max(1, int(max_path_candidates_per_iteration))]:
            if path_idx < 0 or path_idx >= len(paths):
                continue
            try:
                path_width = float(getattr(paths[path_idx], "width", 0.0) or 0.0)
            except Exception:
                path_width = 0.0
            if path_width + 1e-12 < min_repair_width_um:
                continue
            obstacle_bbox = _obstacle_bbox_for_path_rect_shorts(current_plan, shorts, path_idx)
            if obstacle_bbox is None:
                continue
            candidates = _jogged_path_candidates_for_obstacle(
                paths[path_idx],
                obstacle_bbox,
                pdk,
                max_jog_offset_um=max_repair_jog_offset_um,
            )
            if not candidates:
                continue
            for candidate_path in candidates:
                if deadline is not None and time.monotonic() >= deadline:
                    budget_exhausted = True
                    break
                if iteration_evaluations >= max(1, int(max_candidate_evaluations_per_iteration)):
                    break
                iteration_evaluations += 1
                evaluation_count += 1
                candidate_paths = list(paths)
                candidate_paths[path_idx] = candidate_path
                candidate_plan = replace(current_plan, paths=tuple(candidate_paths))
                candidate_plan = snap_oa_write_plan_to_grid(candidate_plan, pdk)
                try:
                    candidate_report = analyze_plan_physical_connectivity(candidate_plan, pdk=pdk)
                except Exception:
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    budget_exhausted = True
                candidate_count = len(tuple(candidate_report.get("shorts", ()) or ()))
                if candidate_count < best_count:
                    best_plan = candidate_plan
                    best_report = candidate_report
                    best_count = candidate_count
                    best_path = candidate_path
                    best_path_idx = path_idx
                    best_change_found = True
                    best_edit = {
                        "kind": "single_path_jog",
                        "path_index": int(path_idx),
                        "net": str(getattr(paths[path_idx], "net", "")),
                        "layer": str(getattr(paths[path_idx], "layer", "")),
                        "old_points": tuple(getattr(paths[path_idx], "points", ()) or ()),
                        "new_points": tuple(getattr(candidate_path, "points", ()) or ()),
                    }
            if iteration_evaluations < max(1, int(max_candidate_evaluations_per_iteration)) and not budget_exhausted:
                cluster_candidates = _cluster_detour_path_candidates_for_obstacle(
                    current_plan,
                    shorts,
                    path_idx,
                    pdk,
                    max_jog_offset_um=max_repair_jog_offset_um,
                )
                for candidate_paths, candidate_edit in cluster_candidates:
                    if deadline is not None and time.monotonic() >= deadline:
                        budget_exhausted = True
                        break
                    if iteration_evaluations >= max(1, int(max_candidate_evaluations_per_iteration)):
                        break
                    iteration_evaluations += 1
                    evaluation_count += 1
                    candidate_plan = replace(current_plan, paths=tuple(candidate_paths))
                    candidate_plan = snap_oa_write_plan_to_grid(candidate_plan, pdk)
                    try:
                        candidate_report = analyze_plan_physical_connectivity(candidate_plan, pdk=pdk)
                    except Exception:
                        continue
                    if deadline is not None and time.monotonic() >= deadline:
                        budget_exhausted = True
                    candidate_count = len(tuple(candidate_report.get("shorts", ()) or ()))
                    if candidate_count < best_count:
                        best_plan = candidate_plan
                        best_report = candidate_report
                        best_count = candidate_count
                        best_path = candidate_paths[-1] if candidate_paths else paths[path_idx]
                        best_path_idx = path_idx
                        best_change_found = True
                        best_edit = dict(candidate_edit)
            if iteration_evaluations < max(1, int(max_candidate_evaluations_per_iteration)) and not budget_exhausted:
                overpass_candidates = _path_path_overpass_candidates_for_short(
                    current_plan,
                    shorts,
                    path_idx,
                    pdk,
                )
                for candidate_paths, candidate_vias, candidate_edit in overpass_candidates:
                    if deadline is not None and time.monotonic() >= deadline:
                        budget_exhausted = True
                        break
                    if iteration_evaluations >= max(1, int(max_candidate_evaluations_per_iteration)):
                        break
                    iteration_evaluations += 1
                    evaluation_count += 1
                    candidate_plan = replace(current_plan, paths=tuple(candidate_paths), vias=tuple(candidate_vias))
                    candidate_plan = snap_oa_write_plan_to_grid(candidate_plan, pdk)
                    try:
                        candidate_report = analyze_plan_physical_connectivity(candidate_plan, pdk=pdk)
                    except Exception:
                        continue
                    if deadline is not None and time.monotonic() >= deadline:
                        budget_exhausted = True
                    candidate_count = len(tuple(candidate_report.get("shorts", ()) or ()))
                    if candidate_count < best_count:
                        best_plan = candidate_plan
                        best_report = candidate_report
                        best_count = candidate_count
                        best_path = candidate_paths[-1] if candidate_paths else paths[path_idx]
                        best_path_idx = path_idx
                        best_change_found = True
                        best_edit = dict(candidate_edit)
            if iteration_evaluations >= max(1, int(max_candidate_evaluations_per_iteration)):
                break
            if budget_exhausted:
                break
        if iteration_evaluations < max(1, int(max_candidate_evaluations_per_iteration)) and not budget_exhausted:
            for candidate_rects, candidate_edit in _structured_access_rect_short_candidates(current_plan, shorts, pdk):
                if deadline is not None and time.monotonic() >= deadline:
                    budget_exhausted = True
                    break
                if iteration_evaluations >= max(1, int(max_candidate_evaluations_per_iteration)):
                    break
                iteration_evaluations += 1
                evaluation_count += 1
                candidate_plan = replace(current_plan, rects=tuple(candidate_rects))
                candidate_plan = snap_oa_write_plan_to_grid(candidate_plan, pdk)
                try:
                    candidate_report = analyze_plan_physical_connectivity(candidate_plan, pdk=pdk)
                except Exception:
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    budget_exhausted = True
                candidate_count = len(tuple(candidate_report.get("shorts", ()) or ()))
                if candidate_count < best_count:
                    best_plan = candidate_plan
                    best_report = candidate_report
                    best_count = candidate_count
                    best_change_found = True
                    best_edit = dict(candidate_edit)
        if budget_exhausted and not best_change_found:
            break
        if not best_change_found:
            break
        path_idx = int(best_path_idx) if best_path_idx is not None else 0
        if best_edit is None:
            best_edit = {
                "kind": "single_path_jog",
                "path_index": int(path_idx),
                "net": str(getattr(paths[path_idx], "net", "")),
                "layer": str(getattr(paths[path_idx], "layer", "")),
                "old_points": tuple(getattr(paths[path_idx], "points", ()) or ()),
                "new_points": tuple(getattr(best_path, "points", ()) or ()),
            }
        best_edit["short_count_before"] = int(current_count)
        best_edit["short_count_after"] = int(best_count)
        edits.append(best_edit)
        current_plan = best_plan
        current_report = best_report
        current_count = best_count
        if current_count == 0:
            break
    return current_plan, {
        "enabled": True,
        "initial_short_count": len(initial_shorts),
        "final_short_count": int(current_count),
        "candidate_evaluation_count": int(evaluation_count),
        "budget_exhausted": bool(budget_exhausted),
        "max_seconds": float(max_seconds or 0.0),
        "edits": tuple(edits),
    }


def repair_isolated_instance_terminal_opens(
    plan: object,
    pdk: object,
    *,
    max_iterations: int = 128,
    max_candidates_per_iteration: int = 128,
    max_bridge_distance_um: float | None = None,
    max_seconds: float | None = None,
) -> tuple[object, Mapping[str, object]]:
    """Bridge isolated PCell terminal bboxes to nearby same-net routed metal.

    This ECO is deliberately narrow.  It only adds a same-layer rectangular
    bridge when an ``instance_terminal`` shape is isolated from every same-net
    non-terminal shape and the bridge reduces the physical open component
    count without increasing shorts.
    """

    from analogskills.eda.oa import snap_oa_write_plan_to_grid
    from analogskills.layout.physical import analyze_plan_physical_connectivity, collect_plan_shapes

    if max_seconds is None:
        try:
            max_seconds = float(get_env("INSTANCE_TERMINAL_OPEN_REPAIR_MAX_SECONDS", "30") or "30")
        except ValueError:
            max_seconds = 30.0
    deadline = time.monotonic() + max_seconds if max_seconds and max_seconds > 0.0 else None
    budget_exhausted = False

    policy = _structured_instance_terminal_open_repair_policy(pdk)
    if max_bridge_distance_um is None:
        max_bridge_distance_um = _dimension_cfg_um(
            policy,
            "max_bridge_distance_um",
            "max_bridge_distance_nm",
            0.75,
        )

    try:
        initial_report = analyze_plan_physical_connectivity(
            plan,
            pdk=pdk,
            include_opens=True,
            include_via_landing_shorts=True,
            include_instance_terminal_shorts=True,
        )
    except Exception:
        return plan, {"enabled": False, "reason": "physical_precheck_unavailable"}

    initial_open_excess = _physical_open_excess(initial_report)
    initial_short_count = _physical_short_count(initial_report)
    if initial_open_excess <= 0:
        return plan, {
            "enabled": True,
            "initial_open_count": 0,
            "final_open_count": 0,
            "initial_open_component_excess": 0,
            "final_open_component_excess": 0,
            "initial_short_count": int(initial_short_count),
            "final_short_count": int(initial_short_count),
            "edits": (),
        }

    current_plan = plan
    current_report = initial_report
    current_short_count = initial_short_count
    current_open_excess = initial_open_excess
    current_open_count = _physical_open_count(initial_report)
    current_geometry_issue_count = _physical_non_connectivity_issue_count(initial_report)
    edits: list[dict[str, object]] = []
    evaluation_count = 0
    batch_enabled = _bool_like(policy.get("batch_enabled", policy.get("enable_batch", True)))
    greedy_accept_first = _bool_like(policy.get("greedy_accept_first_improving", False))

    for _iteration in range(max(0, int(max_iterations))):
        if deadline is not None and time.monotonic() >= deadline:
            budget_exhausted = True
            break
        if current_open_excess <= 0:
            break
        try:
            shapes = collect_plan_shapes(
                current_plan,
                pdk=pdk,
                include_instance_terminals=True,
            )
        except Exception:
            break
        open_nets = _physical_open_nets(current_report)
        candidates = _isolated_instance_terminal_open_bridge_candidates(
            shapes,
            pdk,
            open_nets=open_nets,
            max_bridge_distance_um=float(max_bridge_distance_um or 0.0),
        )
        if not candidates:
            break

        batch_candidates = _nearest_open_bridge_candidate_per_terminal(candidates)
        if batch_enabled and len(batch_candidates) > 1:
            if deadline is not None and time.monotonic() >= deadline:
                budget_exhausted = True
                break
            evaluation_count += 1
            batch_plan = replace(
                current_plan,
                rects=tuple(getattr(current_plan, "rects", ()) or ())
                + tuple(rect for candidate in batch_candidates for rect in _open_bridge_candidate_rects(candidate)),
                vias=tuple(getattr(current_plan, "vias", ()) or ())
                + tuple(via for candidate in batch_candidates for via in _open_bridge_candidate_vias(candidate)),
            )
            batch_plan = snap_oa_write_plan_to_grid(batch_plan, pdk)
            try:
                batch_report = analyze_plan_physical_connectivity(
                    batch_plan,
                    pdk=pdk,
                    include_opens=True,
                    include_via_landing_shorts=True,
                    include_instance_terminal_shorts=True,
                )
            except Exception:
                batch_report = None
            if isinstance(batch_report, Mapping):
                batch_short_count = _physical_short_count(batch_report)
                batch_open_excess = _physical_open_excess(batch_report)
                batch_open_count = _physical_open_count(batch_report)
                batch_geometry_issue_count = _physical_non_connectivity_issue_count(batch_report)
                if (
                    batch_geometry_issue_count <= current_geometry_issue_count
                    and batch_short_count <= current_short_count
                    and batch_open_excess < current_open_excess
                ):
                    edits.append(
                        {
                            "kind": "isolated_instance_terminal_open_bridge_batch",
                            "bridge_count": int(len(batch_candidates)),
                            "open_component_excess_before": int(current_open_excess),
                            "open_component_excess_after": int(batch_open_excess),
                            "open_count_before": int(current_open_count),
                            "open_count_after": int(batch_open_count),
                            "short_count_before": int(current_short_count),
                            "short_count_after": int(batch_short_count),
                            "bridges": tuple(
                                {
                                    "net": candidate["net"],
                                    "layer": candidate["layer"],
                                    "terminal_source": candidate["terminal_source"],
                                    "target_source": candidate["target_source"],
                                    "axis": candidate["axis"],
                                    "distance_um": candidate["distance_um"],
                                    "bbox": tuple(getattr(_open_bridge_candidate_rects(candidate)[0], "bbox", ())),
                                    "bboxes": tuple(tuple(getattr(rect, "bbox", ())) for rect in _open_bridge_candidate_rects(candidate)),
                                    "via_count": len(_open_bridge_candidate_vias(candidate)),
                                }
                                for candidate in batch_candidates
                            ),
                        }
                    )
                    current_plan = batch_plan
                    current_report = batch_report
                    current_short_count = batch_short_count
                    current_open_excess = batch_open_excess
                    current_open_count = batch_open_count
                    continue

        best_plan = current_plan
        best_report = current_report
        best_short_count = current_short_count
        best_open_excess = current_open_excess
        best_open_count = current_open_count
        best_edit: dict[str, object] | None = None
        iteration_evaluations = 0

        for candidate in candidates[: max(1, int(max_candidates_per_iteration))]:
            if deadline is not None and time.monotonic() >= deadline:
                budget_exhausted = True
                break
            iteration_evaluations += 1
            evaluation_count += 1
            candidate_plan = replace(
                current_plan,
                rects=tuple(getattr(current_plan, "rects", ()) or ()) + _open_bridge_candidate_rects(candidate),
                vias=tuple(getattr(current_plan, "vias", ()) or ()) + _open_bridge_candidate_vias(candidate),
            )
            candidate_plan = snap_oa_write_plan_to_grid(candidate_plan, pdk)
            try:
                candidate_report = analyze_plan_physical_connectivity(
                    candidate_plan,
                    pdk=pdk,
                    include_opens=True,
                    include_via_landing_shorts=True,
                    include_instance_terminal_shorts=True,
                )
            except Exception:
                continue
            candidate_short_count = _physical_short_count(candidate_report)
            candidate_open_excess = _physical_open_excess(candidate_report)
            candidate_open_count = _physical_open_count(candidate_report)
            candidate_geometry_issue_count = _physical_non_connectivity_issue_count(candidate_report)
            if candidate_geometry_issue_count > current_geometry_issue_count:
                continue
            if candidate_short_count > current_short_count:
                continue
            if candidate_open_excess >= best_open_excess:
                continue
            best_plan = candidate_plan
            best_report = candidate_report
            best_short_count = candidate_short_count
            best_open_excess = candidate_open_excess
            best_open_count = candidate_open_count
            best_edit = {
                "kind": "isolated_instance_terminal_open_bridge",
                "net": candidate["net"],
                "layer": candidate["layer"],
                "terminal_source": candidate["terminal_source"],
                "target_source": candidate["target_source"],
                "axis": candidate["axis"],
                "distance_um": candidate["distance_um"],
                "bbox": tuple(getattr(_open_bridge_candidate_rects(candidate)[0], "bbox", ())),
                "bboxes": tuple(tuple(getattr(rect, "bbox", ())) for rect in _open_bridge_candidate_rects(candidate)),
                "via_count": len(_open_bridge_candidate_vias(candidate)),
                "open_component_excess_before": int(current_open_excess),
                "open_component_excess_after": int(candidate_open_excess),
                "open_count_before": int(current_open_count),
                "open_count_after": int(candidate_open_count),
                "short_count_before": int(current_short_count),
                "short_count_after": int(candidate_short_count),
            }
            if greedy_accept_first:
                break

        if best_edit is None:
            break
        edits.append(best_edit)
        current_plan = best_plan
        current_report = best_report
        current_short_count = best_short_count
        current_open_excess = best_open_excess
        current_open_count = best_open_count

    return current_plan, {
        "enabled": True,
        "initial_open_count": int(_physical_open_count(initial_report)),
        "final_open_count": int(current_open_count),
        "initial_open_component_excess": int(initial_open_excess),
        "final_open_component_excess": int(current_open_excess),
        "initial_short_count": int(initial_short_count),
        "final_short_count": int(current_short_count),
        "candidate_evaluation_count": int(evaluation_count),
        "budget_exhausted": bool(budget_exhausted),
        "max_seconds": float(max_seconds or 0.0),
        "max_bridge_distance_um": float(max_bridge_distance_um or 0.0),
        "edits": tuple(edits),
    }


def repair_isolated_instance_terminal_opens_to_closure(
    plan: object,
    pdk: object,
    *,
    max_rounds: int = 3,
    max_iterations_per_round: int = 128,
    max_candidates_per_iteration: int = 128,
    max_bridge_distance_um: float | None = None,
    max_seconds_per_round: float | None = None,
) -> tuple[object, Mapping[str, object]]:
    """Run isolated terminal-open repair repeatedly until it converges.

    A single greedy ECO pass can exhaust its time budget on designs with many
    stale terminal-access opens before it reaches late residual opens.  This
    wrapper keeps the same local acceptance rule, but makes closure explicit:
    after each successful pass it recomputes connectivity and starts a fresh
    small search from the improved plan.
    """

    current_plan = plan
    round_summaries: list[dict[str, object]] = []
    total_candidate_evaluations = 0
    total_edits: list[object] = []
    initial_open_count: int | None = None
    initial_open_excess: int | None = None
    initial_short_count: int | None = None
    final_open_count = 0
    final_open_excess = 0
    final_short_count = 0
    stopped_reason = "max_rounds_reached"

    for round_index in range(max(1, int(max_rounds))):
        repaired, summary = repair_isolated_instance_terminal_opens(
            current_plan,
            pdk,
            max_iterations=max_iterations_per_round,
            max_candidates_per_iteration=max_candidates_per_iteration,
            max_bridge_distance_um=max_bridge_distance_um,
            max_seconds=max_seconds_per_round,
        )
        summary_dict = dict(summary)
        summary_dict["round_index"] = int(round_index)
        round_summaries.append(summary_dict)
        total_candidate_evaluations += int(summary_dict.get("candidate_evaluation_count", 0) or 0)
        total_edits.extend(tuple(summary_dict.get("edits", ()) or ()))

        if initial_open_count is None:
            initial_open_count = int(summary_dict.get("initial_open_count", 0) or 0)
            initial_open_excess = int(summary_dict.get("initial_open_component_excess", 0) or 0)
            initial_short_count = int(summary_dict.get("initial_short_count", 0) or 0)
        final_open_count = int(summary_dict.get("final_open_count", 0) or 0)
        final_open_excess = int(summary_dict.get("final_open_component_excess", 0) or 0)
        final_short_count = int(summary_dict.get("final_short_count", 0) or 0)
        current_plan = repaired

        if final_open_excess <= 0:
            stopped_reason = "closed"
            break
        if not tuple(summary_dict.get("edits", ()) or ()):
            stopped_reason = "no_improving_candidate"
            break
        if int(summary_dict.get("final_open_component_excess", 0) or 0) >= int(
            summary_dict.get("initial_open_component_excess", 0) or 0
        ):
            stopped_reason = "no_progress"
            break

    return current_plan, {
        "enabled": True,
        "round_count": int(len(round_summaries)),
        "stopped_reason": stopped_reason,
        "initial_open_count": int(initial_open_count or 0),
        "final_open_count": int(final_open_count),
        "initial_open_component_excess": int(initial_open_excess or 0),
        "final_open_component_excess": int(final_open_excess),
        "initial_short_count": int(initial_short_count or 0),
        "final_short_count": int(final_short_count),
        "candidate_evaluation_count": int(total_candidate_evaluations),
        "budget_exhausted": any(bool(row.get("budget_exhausted", False)) for row in round_summaries),
        "edits": tuple(total_edits),
        "rounds": tuple(round_summaries),
    }


def _structured_instance_terminal_open_repair_policy(pdk: object | None) -> Mapping[str, object]:
    if pdk is None:
        return {}
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    structured = metadata.get("structured_interconnect", {})
    if not isinstance(structured, Mapping):
        return {}
    return _mapping(
        structured.get(
            "isolated_instance_terminal_open_repair",
            structured.get("instance_terminal_open_repair", {}),
        )
    )


def _physical_short_count(report: Mapping[str, object]) -> int:
    return len(tuple(report.get("shorts", ()) or ()))


def _physical_open_count(report: Mapping[str, object]) -> int:
    return len(tuple(report.get("opens", ()) or ()))


def _physical_open_excess(report: Mapping[str, object]) -> int:
    total = 0
    for row in tuple(report.get("opens", ()) or ()):
        if not isinstance(row, Mapping):
            continue
        try:
            total += max(int(row.get("component_count", 0)) - 1, 0)
        except Exception:
            continue
    return total


def _physical_open_nets(report: Mapping[str, object]) -> set[str]:
    nets: set[str] = set()
    for row in tuple(report.get("opens", ()) or ()):
        if isinstance(row, Mapping) and row.get("net"):
            nets.add(str(row.get("net")))
    return nets


def _physical_non_connectivity_issue_count(report: Mapping[str, object]) -> int:
    return (
        len(tuple(report.get("shape_geometry_issues", ()) or ()))
        + len(tuple(report.get("path_geometry_issues", ()) or ()))
        + len(tuple(report.get("via_geometry_issues", ()) or ()))
        + len(tuple(report.get("via_landing_short_issues", ()) or ()))
    )


def _isolated_instance_terminal_open_bridge_candidates(
    shapes: Sequence[object],
    pdk: object,
    *,
    open_nets: set[str],
    max_bridge_distance_um: float,
) -> tuple[Mapping[str, object], ...]:
    from analogskills.eda.oa import OaRect, OaVia

    if max_bridge_distance_um <= 0.0 or not open_nets:
        return ()
    policy = _structured_instance_terminal_open_repair_policy(pdk)
    allow_l_shape = _bool_like(policy.get("allow_l_shape_bridges", False))
    allow_adjacent_via = _bool_like(policy.get("allow_adjacent_layer_via_bridges", False))
    allow_via_stack = _bool_like(policy.get("allow_via_stack_bridges", policy.get("allow_multilayer_via_stack_bridges", False)))
    terminals_by_source: dict[tuple[str, str], list[object]] = {}
    non_terminal_shapes: list[object] = []
    for shape in shapes:
        net = str(getattr(shape, "net", "") or "")
        if not net:
            continue
        if str(getattr(shape, "kind", "") or "") == "instance_terminal":
            if net in open_nets:
                terminals_by_source.setdefault((net, str(getattr(shape, "source", "") or "")), []).append(shape)
        else:
            non_terminal_shapes.append(shape)

    candidates: list[Mapping[str, object]] = []
    seen_rects: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    for (net, terminal_source), terminal_group in sorted(terminals_by_source.items(), key=lambda item: item[0]):
        if not terminal_source:
            continue
        if _terminal_group_touches_nonterminal(terminal_group, non_terminal_shapes):
            continue
        for terminal in terminal_group:
            terminal_layer = str(getattr(terminal, "layer", "") or "")
            terminal_bbox = _shape_bbox_tuple(terminal)
            if not terminal_layer or terminal_bbox is None:
                continue
            same_layer_targets = tuple(
                target
                for target in non_terminal_shapes
                if str(getattr(target, "net", "") or "") == net
                and str(getattr(target, "layer", "") or "") == terminal_layer
            )
            for target in same_layer_targets:
                target_bbox = _shape_bbox_tuple(target)
                if target_bbox is None:
                    continue
                for bbox, axis, distance in _open_bridge_rect_bboxes_for_isolated_terminal(
                    terminal_bbox,
                    target_bbox,
                    pdk,
                    terminal_layer,
                    max_bridge_distance_um=max_bridge_distance_um,
                ):
                    key = (
                        net,
                        terminal_layer,
                        tuple(int(round(float(value) * 1_000_000)) for value in bbox),
                    )
                    if key in seen_rects:
                        continue
                    seen_rects.add(key)
                    candidates.append(
                        {
                            "net": net,
                            "layer": terminal_layer,
                            "terminal_source": terminal_source,
                            "target_source": str(getattr(target, "source", "") or ""),
                            "axis": axis,
                            "distance_um": float(distance),
                            "rect": OaRect(
                                terminal_layer,
                                "drawing",
                                bbox,
                                net=net,
                                metadata={
                                    "kind": "isolated_instance_terminal_open_bridge",
                                    "terminal_source": terminal_source,
                                    "target_source": str(getattr(target, "source", "") or ""),
                                },
                            ),
                            "rects": (
                                OaRect(
                                    terminal_layer,
                                    "drawing",
                                    bbox,
                                    net=net,
                                    metadata={
                                        "kind": "isolated_instance_terminal_open_bridge",
                                        "terminal_source": terminal_source,
                                        "target_source": str(getattr(target, "source", "") or ""),
                                    },
                                ),
                            ),
                            "vias": (),
                        }
                    )
                if allow_l_shape:
                    for rect_bboxes, axis, distance in _open_bridge_l_shape_rect_bboxes_for_isolated_terminal(
                        terminal_bbox,
                        target_bbox,
                        pdk,
                        terminal_layer,
                        max_bridge_distance_um=max_bridge_distance_um,
                    ):
                        key = (
                            net,
                            terminal_layer,
                            tuple(int(round(float(value) * 1_000_000)) for bbox in rect_bboxes for value in bbox),
                        )
                        if key in seen_rects:
                            continue
                        seen_rects.add(key)
                        rects = tuple(
                            OaRect(
                                terminal_layer,
                                "drawing",
                                bbox,
                                net=net,
                                metadata={
                                    "kind": "isolated_instance_terminal_open_l_bridge",
                                    "terminal_source": terminal_source,
                                    "target_source": str(getattr(target, "source", "") or ""),
                                },
                            )
                            for bbox in rect_bboxes
                        )
                        candidates.append(
                            {
                                "net": net,
                                "layer": terminal_layer,
                                "terminal_source": terminal_source,
                                "target_source": str(getattr(target, "source", "") or ""),
                                "axis": axis,
                                "distance_um": float(distance),
                                "rect": rects[0],
                                "rects": rects,
                                "vias": (),
                            }
                        )
            if not allow_adjacent_via:
                continue
            for target in non_terminal_shapes:
                if str(getattr(target, "net", "") or "") != net:
                    continue
                target_layer = str(getattr(target, "layer", "") or "")
                if not target_layer or target_layer == terminal_layer:
                    continue
                via_def = _adjacent_via_def_between_layers(pdk, terminal_layer, target_layer)
                via_stack = _via_stack_between_layers(pdk, terminal_layer, target_layer) if allow_via_stack else ()
                if not via_def and not via_stack:
                    continue
                target_bbox = _shape_bbox_tuple(target)
                if target_bbox is None:
                    continue
                layer_bridge_rows = []
                if via_def:
                    layer_bridge_rows.extend(
                        (
                            rect_bboxes,
                            via_xy,
                            axis,
                            distance,
                            ((via_def, terminal_layer, target_layer),),
                        )
                        for rect_bboxes, via_xy, axis, distance in _open_bridge_adjacent_layer_rect_bboxes_for_isolated_terminal(
                            terminal_bbox,
                            target_bbox,
                            pdk,
                            terminal_layer,
                            target_layer,
                            max_bridge_distance_um=max_bridge_distance_um,
                        )
                    )
                if via_stack:
                    layer_bridge_rows.extend(
                        (
                            rect_bboxes,
                            via_xy,
                            axis,
                            distance,
                            via_stack,
                        )
                        for rect_bboxes, via_xy, axis, distance in _open_bridge_via_stack_rect_bboxes_for_isolated_terminal(
                            terminal_bbox,
                            target_bbox,
                            pdk,
                            terminal_layer,
                            target_layer,
                            via_stack=via_stack,
                            max_bridge_distance_um=max_bridge_distance_um,
                        )
                    )
                for rect_bboxes, via_xy, axis, distance, via_rows in layer_bridge_rows:
                    key = (
                        net,
                        f"{terminal_layer}->{target_layer}",
                        tuple(int(round(float(value) * 1_000_000)) for _layer, bbox in rect_bboxes for value in bbox),
                    )
                    if key in seen_rects:
                        continue
                    seen_rects.add(key)
                    rects = tuple(
                        OaRect(
                            layer,
                            "drawing",
                            bbox,
                            net=net,
                            metadata={
                                "kind": "isolated_instance_terminal_open_via_bridge",
                                "terminal_source": terminal_source,
                                "target_source": str(getattr(target, "source", "") or ""),
                                "via_defs": tuple(row[0] for row in via_rows),
                            },
                        )
                        for layer, bbox in rect_bboxes
                    )
                    vias = tuple(
                        OaVia(
                            via_name,
                            via_xy,
                            net=net,
                            metadata={
                                "kind": "isolated_instance_terminal_open_via_bridge",
                                "terminal_source": terminal_source,
                                "target_source": str(getattr(target, "source", "") or ""),
                                "landing_layers": (lower_layer, upper_layer),
                            },
                        )
                        for via_name, lower_layer, upper_layer in via_rows
                    )
                    candidates.append(
                        {
                            "net": net,
                            "layer": f"{terminal_layer}->{target_layer}",
                            "terminal_source": terminal_source,
                            "target_source": str(getattr(target, "source", "") or ""),
                            "axis": axis,
                            "distance_um": float(distance),
                            "rect": rects[0],
                            "rects": rects,
                            "vias": vias,
                        }
                    )
    return tuple(sorted(candidates, key=_open_bridge_candidate_sort_key))


def _open_bridge_candidate_rects(candidate: Mapping[str, object]) -> tuple[object, ...]:
    rects = candidate.get("rects")
    if isinstance(rects, tuple):
        return rects
    if isinstance(rects, list):
        return tuple(rects)
    rect = candidate.get("rect")
    return () if rect is None else (rect,)


def _open_bridge_candidate_vias(candidate: Mapping[str, object]) -> tuple[object, ...]:
    vias = candidate.get("vias")
    if isinstance(vias, tuple):
        return vias
    if isinstance(vias, list):
        return tuple(vias)
    return ()


def _open_bridge_candidate_sort_key(candidate: Mapping[str, object]) -> tuple[int, int, float, str, str]:
    via_count = len(_open_bridge_candidate_vias(candidate))
    rect_count = len(_open_bridge_candidate_rects(candidate))
    axis = str(candidate.get("axis", "") or "")
    axis_penalty = 0
    if "via_stack" in axis:
        axis_penalty += 3
    elif axis.startswith("via_"):
        axis_penalty += 2
    if "l_shape" in axis:
        axis_penalty += 1
    return (
        int(via_count) * 10 + axis_penalty,
        int(rect_count),
        float(candidate.get("distance_um", 0.0) or 0.0),
        str(candidate.get("net", "") or ""),
        str(candidate.get("terminal_source", "") or ""),
    )


def _nearest_open_bridge_candidate_per_terminal(
    candidates: Sequence[Mapping[str, object]],
) -> tuple[Mapping[str, object], ...]:
    best_by_terminal: dict[tuple[str, str], Mapping[str, object]] = {}
    for candidate in candidates:
        key = (str(candidate.get("net", "") or ""), str(candidate.get("terminal_source", "") or ""))
        if not key[0] or not key[1]:
            continue
        previous = best_by_terminal.get(key)
        if previous is None:
            best_by_terminal[key] = candidate
            continue
        current_rank = (
            float(candidate.get("distance_um", 0.0) or 0.0),
            str(candidate.get("target_source", "") or ""),
            str(candidate.get("axis", "") or ""),
        )
        previous_rank = (
            float(previous.get("distance_um", 0.0) or 0.0),
            str(previous.get("target_source", "") or ""),
            str(previous.get("axis", "") or ""),
        )
        if current_rank < previous_rank:
            best_by_terminal[key] = candidate
    return tuple(
        row
        for _key, row in sorted(
            best_by_terminal.items(),
            key=lambda item: (
                float(item[1].get("distance_um", 0.0) or 0.0),
                item[0][0],
                item[0][1],
            ),
        )
    )


def _terminal_group_touches_nonterminal(terminal_group: Sequence[object], non_terminal_shapes: Sequence[object]) -> bool:
    for terminal in terminal_group:
        terminal_bbox = _shape_bbox_tuple(terminal)
        terminal_net = str(getattr(terminal, "net", "") or "")
        terminal_layer = str(getattr(terminal, "layer", "") or "")
        if terminal_bbox is None or not terminal_net or not terminal_layer:
            continue
        for target in non_terminal_shapes:
            if str(getattr(target, "net", "") or "") != terminal_net:
                continue
            if str(getattr(target, "layer", "") or "") != terminal_layer:
                continue
            target_bbox = _shape_bbox_tuple(target)
            if target_bbox is not None and _bbox_overlap_um(terminal_bbox, target_bbox, include_touching=True):
                return True
    return False


def _shape_bbox_tuple(shape: object) -> tuple[float, float, float, float] | None:
    try:
        bbox = tuple(float(value) for value in getattr(shape, "bbox"))
    except Exception:
        return None
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return (bbox[0], bbox[1], bbox[2], bbox[3])


def _open_bridge_rect_bboxes_for_isolated_terminal(
    terminal_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
    pdk: object,
    layer: str,
    *,
    max_bridge_distance_um: float,
) -> tuple[tuple[tuple[float, float, float, float], str, float], ...]:
    """Return short same-layer bridge rects between aligned terminal/target boxes."""

    if _bbox_overlap_um(terminal_bbox, target_bbox, include_touching=True):
        return ()
    grid = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 0.001)
    min_width = max(_min_width_um(pdk, layer, 0.05), grid)
    contact_overlap = max(grid, min_width * 0.5)
    candidates: list[tuple[tuple[float, float, float, float], str, float]] = []

    tx0, ty0, tx1, ty1 = terminal_bbox
    rx0, ry0, rx1, ry1 = target_bbox
    vertical_overlap = min(ty1, ry1) - max(ty0, ry0)
    if vertical_overlap >= -1e-12:
        if tx1 <= rx0:
            distance = rx0 - tx1
            x0 = tx1 - contact_overlap
            x1 = rx0 + contact_overlap
        elif rx1 <= tx0:
            distance = tx0 - rx1
            x0 = rx1 - contact_overlap
            x1 = tx0 + contact_overlap
        else:
            distance = 0.0
            x0 = min(tx0, rx0)
            x1 = max(tx1, rx1)
        if 0.0 <= distance <= max_bridge_distance_um:
            overlap_lo = max(ty0, ry0)
            overlap_hi = min(ty1, ry1)
            center_y = (overlap_lo + overlap_hi) * 0.5 if overlap_hi >= overlap_lo else (ty0 + ty1) * 0.5
            width_y = min_width
            bbox = (x0, center_y - width_y * 0.5, x1, center_y + width_y * 0.5)
            bbox = _snap_bbox(pdk, _ensure_bbox_min_side(bbox, min_width))
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                candidates.append((bbox, "horizontal", float(distance)))

    horizontal_overlap = min(tx1, rx1) - max(tx0, rx0)
    if horizontal_overlap >= -1e-12:
        if ty1 <= ry0:
            distance = ry0 - ty1
            y0 = ty1 - contact_overlap
            y1 = ry0 + contact_overlap
        elif ry1 <= ty0:
            distance = ty0 - ry1
            y0 = ry1 - contact_overlap
            y1 = ty0 + contact_overlap
        else:
            distance = 0.0
            y0 = min(ty0, ry0)
            y1 = max(ty1, ry1)
        if 0.0 <= distance <= max_bridge_distance_um:
            overlap_lo = max(tx0, rx0)
            overlap_hi = min(tx1, rx1)
            center_x = (overlap_lo + overlap_hi) * 0.5 if overlap_hi >= overlap_lo else (tx0 + tx1) * 0.5
            width_x = min_width
            bbox = (center_x - width_x * 0.5, y0, center_x + width_x * 0.5, y1)
            bbox = _snap_bbox(pdk, _ensure_bbox_min_side(bbox, min_width))
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                candidates.append((bbox, "vertical", float(distance)))

    unique: dict[tuple[int, int, int, int], tuple[tuple[float, float, float, float], str, float]] = {}
    for bbox, axis, distance in candidates:
        key = tuple(int(round(value * 1_000_000)) for value in bbox)
        unique.setdefault(key, (bbox, axis, distance))
    return tuple(unique.values())


def _open_bridge_l_shape_rect_bboxes_for_isolated_terminal(
    terminal_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
    pdk: object,
    layer: str,
    *,
    max_bridge_distance_um: float,
) -> tuple[tuple[tuple[tuple[float, float, float, float], ...], str, float], ...]:
    """Return two-rect same-layer L bridges for non-aligned isolated terminals."""

    if _bbox_overlap_um(terminal_bbox, target_bbox, include_touching=True):
        return ()
    grid = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 0.001)
    min_width = max(_min_width_um(pdk, layer, 0.05), grid)
    contact_overlap = max(grid, min_width * 0.5)
    tx0, ty0, tx1, ty1 = terminal_bbox
    rx0, ry0, rx1, ry1 = target_bbox
    tcx, tcy = ((tx0 + tx1) * 0.5, (ty0 + ty1) * 0.5)
    rcx, rcy = ((rx0 + rx1) * 0.5, (ry0 + ry1) * 0.5)
    dx = max(rx0 - tx1, tx0 - rx1, 0.0)
    dy = max(ry0 - ty1, ty0 - ry1, 0.0)
    distance = dx + dy
    if distance <= 0.0 or distance > max_bridge_distance_um:
        return ()

    # Route from the terminal center vertically to the target center Y, then
    # horizontally to the target center X.  Both rectangles overlap at the
    # elbow and overlap their endpoint boxes by contact_overlap.
    vertical = _snap_bbox(
        pdk,
        _ensure_bbox_min_side(
            (
                tcx - min_width * 0.5,
                min(tcy, rcy) - contact_overlap,
                tcx + min_width * 0.5,
                max(tcy, rcy) + contact_overlap,
            ),
            min_width,
        ),
    )
    horizontal = _snap_bbox(
        pdk,
        _ensure_bbox_min_side(
            (
                min(tcx, rcx) - contact_overlap,
                rcy - min_width * 0.5,
                max(tcx, rcx) + contact_overlap,
                rcy + min_width * 0.5,
            ),
            min_width,
        ),
    )
    if vertical[2] <= vertical[0] or vertical[3] <= vertical[1] or horizontal[2] <= horizontal[0] or horizontal[3] <= horizontal[1]:
        return ()
    return (((vertical, horizontal), "l_shape", float(distance)),)


def _open_bridge_adjacent_layer_rect_bboxes_for_isolated_terminal(
    terminal_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
    pdk: object,
    terminal_layer: str,
    target_layer: str,
    *,
    max_bridge_distance_um: float,
) -> tuple[tuple[tuple[tuple[str, tuple[float, float, float, float]], ...], tuple[float, float], str, float], ...]:
    """Return terminal-layer landing plus target-layer straight/L bridge via patches."""

    if _bbox_overlap_um(terminal_bbox, target_bbox, include_touching=True):
        return ()
    grid = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 0.001)
    terminal_width = max(_min_width_um(pdk, terminal_layer, 0.05), grid)
    target_width = max(_min_width_um(pdk, target_layer, 0.05), grid)
    result: list[tuple[tuple[tuple[str, tuple[float, float, float, float]], ...], tuple[float, float], str, float]] = []
    for via_xy in _terminal_escape_via_points(terminal_bbox, pdk, terminal_layer):
        landing = _snap_bbox(
            pdk,
            _ensure_bbox_min_side(
                (
                    via_xy[0] - terminal_width * 0.5,
                    via_xy[1] - terminal_width * 0.5,
                    via_xy[0] + terminal_width * 0.5,
                    via_xy[1] + terminal_width * 0.5,
                ),
                terminal_width,
            ),
        )
        via_target_bbox = _snap_bbox(
            pdk,
            _ensure_bbox_min_side(
                (
                    via_xy[0] - target_width * 0.5,
                    via_xy[1] - target_width * 0.5,
                    via_xy[0] + target_width * 0.5,
                    via_xy[1] + target_width * 0.5,
                ),
                target_width,
            ),
        )
        straight = _open_bridge_rect_bboxes_for_isolated_terminal(
            via_target_bbox,
            target_bbox,
            pdk,
            target_layer,
            max_bridge_distance_um=max_bridge_distance_um,
        )
        for bbox, axis, distance in straight:
            result.append((((terminal_layer, landing), (target_layer, bbox)), via_xy, f"via_{axis}", float(distance)))
        l_shapes = _open_bridge_l_shape_rect_bboxes_for_isolated_terminal(
            via_target_bbox,
            target_bbox,
            pdk,
            target_layer,
            max_bridge_distance_um=max_bridge_distance_um,
        )
        for bboxes, axis, distance in l_shapes:
            result.append(
                (
                    tuple((terminal_layer, landing) for _ in (0,)) + tuple((target_layer, bbox) for bbox in bboxes),
                    via_xy,
                    f"via_{axis}",
                    float(distance),
                )
            )
    unique: dict[tuple[str, tuple[int, ...]], tuple[tuple[tuple[str, tuple[float, float, float, float]], ...], tuple[float, float], str, float]] = {}
    for rects, xy, axis, distance in result:
        key = (
            axis,
            tuple(int(round(value * 1_000_000)) for value in (*xy,)),
            tuple(int(round(value * 1_000_000)) for _layer, bbox in rects for value in bbox),
        )
        unique.setdefault(key, (rects, xy, axis, distance))
    return tuple(unique.values())


def _open_bridge_via_stack_rect_bboxes_for_isolated_terminal(
    terminal_bbox: tuple[float, float, float, float],
    target_bbox: tuple[float, float, float, float],
    pdk: object,
    terminal_layer: str,
    target_layer: str,
    *,
    via_stack: Sequence[tuple[str, str, str]],
    max_bridge_distance_um: float,
) -> tuple[tuple[tuple[tuple[str, tuple[float, float, float, float]], ...], tuple[float, float], str, float], ...]:
    if not via_stack:
        return ()
    stack_layers = _metal_layers_between(pdk, terminal_layer, target_layer)
    if len(stack_layers) < 2:
        return ()
    grid = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 0.001)
    result: list[tuple[tuple[tuple[str, tuple[float, float, float, float]], ...], tuple[float, float], str, float]] = []
    for via_xy in _terminal_escape_via_points(terminal_bbox, pdk, terminal_layer):
        landing_rects: list[tuple[str, tuple[float, float, float, float]]] = []
        for layer in stack_layers:
            width = max(_min_width_um(pdk, layer, 0.05), grid)
            landing_rects.append(
                (
                    layer,
                    _snap_bbox(
                        pdk,
                        _ensure_bbox_min_side(
                            (
                                via_xy[0] - width * 0.5,
                                via_xy[1] - width * 0.5,
                                via_xy[0] + width * 0.5,
                                via_xy[1] + width * 0.5,
                            ),
                            width,
                        ),
                    ),
                )
            )
        target_landing = landing_rects[-1][1]
        straight = _open_bridge_rect_bboxes_for_isolated_terminal(
            target_landing,
            target_bbox,
            pdk,
            target_layer,
            max_bridge_distance_um=max_bridge_distance_um,
        )
        for bbox, axis, distance in straight:
            result.append((tuple(landing_rects[:-1]) + ((target_layer, bbox),), via_xy, f"via_stack_{axis}", float(distance)))
        l_shapes = _open_bridge_l_shape_rect_bboxes_for_isolated_terminal(
            target_landing,
            target_bbox,
            pdk,
            target_layer,
            max_bridge_distance_um=max_bridge_distance_um,
        )
        for bboxes, axis, distance in l_shapes:
            result.append(
                (
                    tuple(landing_rects[:-1]) + tuple((target_layer, bbox) for bbox in bboxes),
                    via_xy,
                    f"via_stack_{axis}",
                    float(distance),
                )
            )
    unique: dict[tuple[str, tuple[int, ...]], tuple[tuple[tuple[str, tuple[float, float, float, float]], ...], tuple[float, float], str, float]] = {}
    for rects, xy, axis, distance in result:
        key = (
            axis,
            tuple(int(round(value * 1_000_000)) for value in (*xy,)),
            tuple(int(round(value * 1_000_000)) for _layer, bbox in rects for value in bbox),
        )
        unique.setdefault(key, (rects, xy, axis, distance))
    return tuple(unique.values())


def _terminal_escape_via_points(
    terminal_bbox: tuple[float, float, float, float],
    pdk: object,
    terminal_layer: str,
) -> tuple[tuple[float, float], ...]:
    """Return candidate via-stack origins inside a terminal bbox.

    Center-only via placement is brittle for compact analog access: the center
    of a real shared-diffusion terminal can be occupied by an old access strap
    from a neighboring terminal after master replacement.  These points keep
    the ECO local to the terminal bbox but let the checker choose an edge/corner
    escape that avoids intermediate-layer shorts.
    """

    tx0, ty0, tx1, ty1 = (float(value) for value in terminal_bbox)
    if tx1 <= tx0 or ty1 <= ty0:
        return ()
    grid = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 0.001)
    width_x = tx1 - tx0
    width_y = ty1 - ty0
    min_width = max(_min_width_um(pdk, terminal_layer, 0.05), grid)
    inset = max(grid, min(min(width_x, width_y) * 0.25, min_width))
    cx = (tx0 + tx1) * 0.5
    cy = (ty0 + ty1) * 0.5
    x_left = min(max(tx0 + inset, tx0), tx1)
    x_right = min(max(tx1 - inset, tx0), tx1)
    y_bottom = min(max(ty0 + inset, ty0), ty1)
    y_top = min(max(ty1 - inset, ty0), ty1)
    raw_points = (
        (cx, cy),
        (x_right, cy),
        (x_left, cy),
        (cx, y_top),
        (cx, y_bottom),
        (x_right, y_top),
        (x_right, y_bottom),
        (x_left, y_top),
        (x_left, y_bottom),
    )
    policy = _structured_instance_terminal_open_repair_policy(pdk)
    limit = _int_cfg(policy, "max_terminal_escape_points", 9, minimum=1)
    unique: dict[tuple[int, int], tuple[float, float]] = {}
    for point in raw_points:
        snapped = _snap_point(pdk, point)
        if snapped[0] < tx0 - grid or snapped[0] > tx1 + grid or snapped[1] < ty0 - grid or snapped[1] > ty1 + grid:
            continue
        key = (int(round(snapped[0] * 1_000_000)), int(round(snapped[1] * 1_000_000)))
        unique.setdefault(key, snapped)
        if len(unique) >= limit:
            break
    return tuple(unique.values())


def _adjacent_via_def_between_layers(pdk: object, layer_a: str, layer_b: str) -> str:
    layer_a = str(layer_a)
    layer_b = str(layer_b)
    via_stack = tuple(getattr(pdk, "via_stack", ()) or ())
    for rule in via_stack:
        lower = str(getattr(rule, "lower_layer", "") or "")
        upper = str(getattr(rule, "upper_layer", "") or "")
        if {lower, upper} == {layer_a, layer_b}:
            return str(getattr(rule, "via_def", "") or "")
    layer_map = getattr(pdk, "layer_map", None)
    metals = tuple(str(layer) for layer in tuple(getattr(layer_map, "metals", ()) or ()))
    vias = tuple(str(via) for via in tuple(getattr(layer_map, "vias", ()) or ()))
    if layer_a in metals and layer_b in metals:
        ia, ib = metals.index(layer_a), metals.index(layer_b)
        if abs(ia - ib) == 1:
            idx = min(ia, ib)
            if idx < len(vias):
                return vias[idx]
    return ""


def _via_stack_between_layers(pdk: object, layer_a: str, layer_b: str) -> tuple[tuple[str, str, str], ...]:
    layers = _metal_layers_between(pdk, layer_a, layer_b)
    if len(layers) < 2:
        return ()
    rows: list[tuple[str, str, str]] = []
    for lower, upper in zip(layers, layers[1:]):
        via_def = _adjacent_via_def_between_layers(pdk, lower, upper)
        if not via_def:
            return ()
        rows.append((via_def, lower, upper))
    return tuple(rows)


def _metal_layers_between(pdk: object, layer_a: str, layer_b: str) -> tuple[str, ...]:
    layer_map = getattr(pdk, "layer_map", None)
    metals = tuple(str(layer) for layer in tuple(getattr(layer_map, "metals", ()) or ()))
    if layer_a not in metals or layer_b not in metals:
        return ()
    ia, ib = metals.index(layer_a), metals.index(layer_b)
    step = 1 if ib >= ia else -1
    return tuple(metals[idx] for idx in range(ia, ib + step, step))


def _ensure_bbox_min_side(
    bbox: tuple[float, float, float, float],
    min_side: float,
) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = bbox
    if x1 - x0 < min_side:
        cx = (x0 + x1) * 0.5
        x0 = cx - min_side * 0.5
        x1 = cx + min_side * 0.5
    if y1 - y0 < min_side:
        cy = (y0 + y1) * 0.5
        y0 = cy - min_side * 0.5
        y1 = cy + min_side * 0.5
    return (x0, y0, x1, y1)


def _path_index_with_most_rect_shorts(shorts: Sequence[Mapping[str, object]]) -> int | None:
    indices = _path_indices_by_rect_short_count(shorts)
    return indices[0] if indices else None


def _structured_short_jog_repair_policy(pdk: object | None) -> Mapping[str, object]:
    if pdk is None:
        return {}
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    structured = metadata.get("structured_interconnect", {})
    if not isinstance(structured, Mapping):
        return {}
    return _mapping(structured.get("local_short_jog_repair", structured.get("short_jog_repair", {})))


def _path_indices_by_rect_short_count(shorts: Sequence[Mapping[str, object]]) -> tuple[int, ...]:
    counts: dict[int, int] = {}
    for short in shorts:
        path_a = _source_index(short.get("source_a"), "path")
        path_b = _source_index(short.get("source_b"), "path")
        rect_a = _source_index(short.get("source_a"), "rect")
        rect_b = _source_index(short.get("source_b"), "rect")
        if path_a is not None and (rect_b is not None or path_b is not None):
            counts[path_a] = counts.get(path_a, 0) + 1
        if path_b is not None and (rect_a is not None or path_a is not None) and path_b != path_a:
            counts[path_b] = counts.get(path_b, 0) + 1
    if not counts:
        return ()
    return tuple(path_idx for path_idx, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _obstacle_bbox_for_path_rect_shorts(plan: object, shorts: Sequence[Mapping[str, object]], path_idx: int) -> tuple[float, float, float, float] | None:
    rect_indices: list[int] = []
    path_sources: list[object] = []
    for short in shorts:
        source_a = short.get("source_a")
        source_b = short.get("source_b")
        if _source_index(source_a, "path") == path_idx:
            rect_idx = _source_index(source_b, "rect")
            if rect_idx is None and _source_index(source_b, "path") is not None:
                path_sources.append(source_b)
        elif _source_index(source_b, "path") == path_idx:
            rect_idx = _source_index(source_a, "rect")
            if rect_idx is None and _source_index(source_a, "path") is not None:
                path_sources.append(source_a)
        else:
            rect_idx = None
        if rect_idx is not None:
            rect_indices.append(rect_idx)
    rects = tuple(getattr(plan, "rects", ()) or ())
    paths = tuple(getattr(plan, "paths", ()) or ())
    bboxes = []
    for rect_idx in sorted(set(rect_indices)):
        if 0 <= rect_idx < len(rects):
            try:
                bboxes.append(tuple(float(v) for v in getattr(rects[rect_idx], "bbox")))
            except Exception:
                continue
    for source in path_sources:
        other_path_idx = _source_index(source, "path")
        if other_path_idx is None or other_path_idx == path_idx or not (0 <= other_path_idx < len(paths)):
            continue
        path_bbox = _path_source_bbox(paths[other_path_idx], source)
        if path_bbox is not None:
            bboxes.append(path_bbox)
    if not bboxes:
        return None
    return (
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    )


def _source_segment_index(source: object) -> int | None:
    text = str(source or "")
    marker = ".segment["
    if marker not in text:
        return None
    try:
        return int(text.split(marker, 1)[1].split("]", 1)[0])
    except ValueError:
        return None


def _path_source_bbox(path_obj: object, source: object) -> tuple[float, float, float, float] | None:
    try:
        points = tuple((float(point[0]), float(point[1])) for point in tuple(getattr(path_obj, "points", ()) or ()))
        width = max(float(getattr(path_obj, "width", 0.0) or 0.0), 0.0)
    except Exception:
        return None
    if len(points) < 2:
        return None
    segment_index = _source_segment_index(source)
    if segment_index is not None and 0 <= segment_index < len(points) - 1:
        segment_points = (points[segment_index], points[segment_index + 1])
    else:
        segment_points = points
    half = width * 0.5
    return (
        min(point[0] for point in segment_points) - half,
        min(point[1] for point in segment_points) - half,
        max(point[0] for point in segment_points) + half,
        max(point[1] for point in segment_points) + half,
    )


def _source_index(source: object, kind: str) -> int | None:
    text = str(source or "")
    prefix = f"{kind}["
    if not text.startswith(prefix):
        return None
    try:
        return int(text[len(prefix) :].split("]", 1)[0])
    except ValueError:
        return None


def _bbox_overlap_um(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
    *,
    include_touching: bool = True,
) -> bool:
    if include_touching:
        return not (left[2] < right[0] or right[2] < left[0] or left[3] < right[1] or right[3] < left[1])
    return not (left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1])


def _jogged_path_candidates_for_obstacle(
    path_obj: object,
    obstacle_bbox: tuple[float, float, float, float],
    pdk: object,
    *,
    max_jog_offset_um: float = 2.0,
) -> tuple[object, ...]:
    points = tuple(getattr(path_obj, "points", ()) or ())
    if len(points) < 2:
        return ()
    try:
        normalized_points = tuple((float(point[0]), float(point[1])) for point in points)
        width = max(float(getattr(path_obj, "width", 0.0) or 0.0), 0.0)
    except Exception:
        return ()
    layer = str(getattr(path_obj, "layer", "") or "")
    spacing = _rule_spacing_um(pdk, layer, 0.10)
    margin = max(width + spacing + 0.08, 0.25)
    x_lo, y_lo, x_hi, y_hi = obstacle_bbox
    offset = max(width + spacing + max(x_hi - x_lo, y_hi - y_lo) + 0.20, 0.50)
    if max_jog_offset_um > 0.0:
        offset = min(offset, max(float(max_jog_offset_um), 0.50))
    candidates = []

    def append_candidate(segment_index: int, segment_points: tuple[tuple[float, float], ...]) -> None:
        raw_points = (
            *normalized_points[:segment_index],
            *segment_points,
            *normalized_points[segment_index + 2 :],
        )
        deduped: list[tuple[float, float]] = []
        for point in raw_points:
            if deduped and abs(deduped[-1][0] - point[0]) <= 1e-12 and abs(deduped[-1][1] - point[1]) <= 1e-12:
                continue
            deduped.append(point)
        if len(deduped) >= 2:
            candidates.append(replace(path_obj, points=tuple(deduped)))

    for segment_index, ((x0, y0), (x1, y1)) in enumerate(zip(normalized_points, normalized_points[1:])):
        segment_bbox = (
            min(x0, x1) - width * 0.5,
            min(y0, y1) - width * 0.5,
            max(x0, x1) + width * 0.5,
            max(y0, y1) + width * 0.5,
        )
        if not _bbox_overlap_um(segment_bbox, obstacle_bbox):
            continue
        if abs(x0 - x1) <= 1e-9 and abs(y0 - y1) > 1e-9:
            seg_lo, seg_hi = sorted((y0, y1))
            low = max(seg_lo, y_lo - margin)
            high = min(seg_hi, y_hi + margin)
            if high - low > 1e-9 and low > seg_lo + 1e-9 and high < seg_hi - 1e-9:
                for sign in (1.0, -1.0, 2.0, -2.0):
                    jog_x = x0 + sign * offset
                    enter = low if y0 < y1 else high
                    leave = high if y0 < y1 else low
                    append_candidate(
                        segment_index,
                        ((x0, y0), (x0, enter), (jog_x, enter), (jog_x, leave), (x0, leave), (x1, y1)),
                    )
            else:
                # Obstacle touches or nearly touches a segment endpoint.  This
                # is common in dense resistor ladders where the short is against
                # a neighboring terminal landing; the old internal-window jog
                # skipped those cases.  Try a whole-segment dogleg and let the
                # physical precheck decide whether it is an improvement.
                for sign in (1.0, -1.0, 2.0, -2.0):
                    jog_x = x0 + sign * offset
                    append_candidate(
                        segment_index,
                        ((x0, y0), (jog_x, y0), (jog_x, y1), (x1, y1)),
                    )
        elif abs(y0 - y1) <= 1e-9 and abs(x0 - x1) > 1e-9:
            seg_lo, seg_hi = sorted((x0, x1))
            low = max(seg_lo, x_lo - margin)
            high = min(seg_hi, x_hi + margin)
            if high - low > 1e-9 and low > seg_lo + 1e-9 and high < seg_hi - 1e-9:
                for sign in (1.0, -1.0, 2.0, -2.0):
                    jog_y = y0 + sign * offset
                    enter = low if x0 < x1 else high
                    leave = high if x0 < x1 else low
                    append_candidate(
                        segment_index,
                        ((x0, y0), (enter, y0), (enter, jog_y), (leave, jog_y), (leave, y0), (x1, y1)),
                    )
            else:
                for sign in (1.0, -1.0, 2.0, -2.0):
                    jog_y = y0 + sign * offset
                    append_candidate(
                        segment_index,
                        ((x0, y0), (x0, jog_y), (x1, jog_y), (x1, y1)),
                    )
    return tuple(candidates)


def _path_path_overpass_candidates_for_short(
    plan: object,
    shorts: Sequence[Mapping[str, object]],
    path_idx: int,
    pdk: object,
) -> tuple[tuple[tuple[object, ...], tuple[object, ...], dict[str, object]], ...]:
    paths = tuple(getattr(plan, "paths", ()) or ())
    vias = tuple(getattr(plan, "vias", ()) or ())
    if path_idx < 0 or path_idx >= len(paths):
        return ()
    victim = paths[path_idx]
    victim_points = _path_points_tuple(victim)
    if len(victim_points) != 2:
        return ()
    layer = str(getattr(victim, "layer", "") or "")
    net = str(getattr(victim, "net", "") or "")
    if not layer or not net:
        return ()
    overpass = _adjacent_overpass_layer_and_via(pdk, layer)
    if overpass is None:
        return ()
    bridge_layer, via_def = overpass
    try:
        victim_width = max(float(getattr(victim, "width", 0.0) or 0.0), 0.0)
    except Exception:
        victim_width = 0.0
    if victim_width <= 0.0:
        return ()
    candidates: list[tuple[tuple[object, ...], tuple[object, ...], dict[str, object]]] = []
    for short in shorts:
        source_a = short.get("source_a")
        source_b = short.get("source_b")
        if _source_index(source_a, "path") == path_idx:
            obstacle_source = source_b
        elif _source_index(source_b, "path") == path_idx:
            obstacle_source = source_a
        else:
            continue
        obstacle_idx = _source_index(obstacle_source, "path")
        if obstacle_idx is None or obstacle_idx == path_idx or not (0 <= obstacle_idx < len(paths)):
            continue
        obstacle = paths[obstacle_idx]
        if str(getattr(obstacle, "layer", "") or "") != layer:
            continue
        if str(getattr(obstacle, "net", "") or "") == net:
            continue
        obstacle_bbox = _path_source_bbox(obstacle, obstacle_source)
        if obstacle_bbox is None:
            continue
        for replacement_paths, via_points, repair_kind in _overpass_replacement_paths(
            victim,
            bridge_layer,
            obstacle_bbox,
            pdk,
        ):
            if not replacement_paths or not via_points:
                continue
            candidate_paths = tuple(path for idx, path in enumerate(paths) if idx != path_idx) + tuple(replacement_paths)
            candidate_vias = vias + tuple(_make_oa_via(via_def, point, net) for point in via_points)
            candidates.append(
                (
                    tuple(candidate_paths),
                    tuple(candidate_vias),
                    {
                        "kind": "same_layer_path_overpass",
                        "path_index": int(path_idx),
                        "obstacle_path_index": int(obstacle_idx),
                        "net": net,
                        "obstacle_net": str(getattr(obstacle, "net", "") or ""),
                        "layer": layer,
                        "bridge_layer": bridge_layer,
                        "via_def": via_def,
                        "repair_kind": repair_kind,
                        "old_points": tuple(getattr(victim, "points", ()) or ()),
                        "new_path_count": int(len(replacement_paths)),
                        "via_points": tuple(via_points),
                        "obstacle_bbox": tuple(float(v) for v in obstacle_bbox),
                    },
                )
            )
    return tuple(candidates)


def _overpass_replacement_paths(
    victim: object,
    bridge_layer: str,
    obstacle_bbox: tuple[float, float, float, float],
    pdk: object,
) -> tuple[tuple[tuple[object, ...], tuple[tuple[float, float], ...], str], ...]:
    points = _path_points_tuple(victim)
    if len(points) != 2:
        return ()
    (x0, y0), (x1, y1) = points
    layer = str(getattr(victim, "layer", "") or "")
    width = max(float(getattr(victim, "width", 0.0) or 0.0), 0.0)
    spacing = _rule_spacing_um(pdk, layer, 0.10)
    cut_margin = max(width + spacing + 0.08, 0.25)
    candidates: list[tuple[tuple[object, ...], tuple[tuple[float, float], ...], str]] = []

    def append(paths: Sequence[object], via_points: Sequence[tuple[float, float]], kind: str) -> None:
        if len(via_points) != 2:
            return
        candidates.append((tuple(paths), tuple(via_points), kind))

    if abs(y0 - y1) <= 1e-9 and abs(x0 - x1) > 1e-9:
        lo, hi = sorted((x0, x1))
        cut_lo = max(lo, obstacle_bbox[0] - cut_margin)
        cut_hi = min(hi, obstacle_bbox[2] + cut_margin)
        if cut_hi - cut_lo <= max(width, 0.02):
            return ()
        left_point = (cut_lo, y0)
        right_point = (cut_hi, y0)
        new_paths: list[object] = []
        if cut_lo - lo > 1e-9:
            new_paths.append(replace(victim, points=((lo, y0), left_point)))
        if hi - cut_hi > 1e-9:
            new_paths.append(replace(victim, points=(right_point, (hi, y0))))
        new_paths.append(replace(victim, layer=bridge_layer, points=(left_point, right_point)))
        append(new_paths, (left_point, right_point), "horizontal_overpass")
    elif abs(x0 - x1) <= 1e-9 and abs(y0 - y1) > 1e-9:
        lo, hi = sorted((y0, y1))
        cut_lo = max(lo, obstacle_bbox[1] - cut_margin)
        cut_hi = min(hi, obstacle_bbox[3] + cut_margin)
        if cut_hi - cut_lo <= max(width, 0.02):
            return ()
        bottom_point = (x0, cut_lo)
        top_point = (x0, cut_hi)
        new_paths = []
        if cut_lo - lo > 1e-9:
            new_paths.append(replace(victim, points=((x0, lo), bottom_point)))
        if hi - cut_hi > 1e-9:
            new_paths.append(replace(victim, points=(top_point, (x0, hi))))
        new_paths.append(replace(victim, layer=bridge_layer, points=(bottom_point, top_point)))
        append(new_paths, (bottom_point, top_point), "vertical_overpass")
    return tuple(candidates)


def _adjacent_overpass_layer_and_via(pdk: object, layer: str) -> tuple[str, str] | None:
    metals = tuple(str(item) for item in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    layer = str(layer)
    if layer not in metals:
        return None
    idx = metals.index(layer)
    if idx + 1 < len(metals):
        lower = layer
        upper = metals[idx + 1]
        stack = tuple(_via_stack_between(pdk, lower, upper, metals))
        if stack:
            return (upper, str(stack[0][1]))
    if idx - 1 >= 0:
        lower = metals[idx - 1]
        upper = layer
        stack = tuple(_via_stack_between(pdk, lower, upper, metals))
        if stack:
            return (lower, str(stack[0][1]))
    return None


def _make_oa_via(via_def: str, point: tuple[float, float], net: str) -> object:
    from analogskills.eda.oa import OaVia

    return OaVia(str(via_def), (float(point[0]), float(point[1])), str(net))


def _structured_access_rect_short_candidates(
    plan: object,
    shorts: Sequence[Mapping[str, object]],
    pdk: object,
) -> tuple[tuple[tuple[object, ...], dict[str, object]], ...]:
    rects = tuple(getattr(plan, "rects", ()) or ())
    if not rects:
        return ()
    candidates: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for short in shorts:
        rect_a = _source_index(short.get("source_a"), "rect")
        rect_b = _source_index(short.get("source_b"), "rect")
        if rect_a is None or rect_b is None:
            continue
        if not (0 <= rect_a < len(rects) and 0 <= rect_b < len(rects)):
            continue
        for mutable_idx, fixed_idx in ((rect_a, rect_b), (rect_b, rect_a)):
            mutable = rects[mutable_idx]
            fixed = rects[fixed_idx]
            if not _is_structured_terminal_access_rect(mutable):
                continue
            layer = str(getattr(mutable, "layer", "") or "")
            if not layer or layer != str(getattr(fixed, "layer", "") or ""):
                continue
            mutable_bbox = _rect_bbox_tuple(mutable)
            fixed_bbox = _rect_bbox_tuple(fixed)
            if mutable_bbox is None or fixed_bbox is None:
                continue
            for new_bbox, repair_kind in _access_rect_trim_bboxes_for_obstacle(mutable_bbox, fixed_bbox, pdk, layer):
                updated = list(rects)
                updated[mutable_idx] = replace(mutable, bbox=new_bbox)
                candidates.append(
                    (
                        tuple(updated),
                        {
                            "kind": "structured_access_rect_trim",
                            "rect_index": int(mutable_idx),
                            "fixed_rect_index": int(fixed_idx),
                            "net": str(getattr(mutable, "net", "") or ""),
                            "fixed_net": str(getattr(fixed, "net", "") or ""),
                            "layer": layer,
                            "repair_kind": repair_kind,
                            "old_bbox": tuple(float(v) for v in mutable_bbox),
                            "new_bbox": tuple(float(v) for v in new_bbox),
                            "fixed_bbox": tuple(float(v) for v in fixed_bbox),
                        },
                    )
                )
    return tuple(candidates)


def _is_structured_terminal_access_rect(rect: object) -> bool:
    metadata = getattr(rect, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return False
    return str(metadata.get("kind", "") or "") == "structured_terminal_access"


def _rect_bbox_tuple(rect: object) -> tuple[float, float, float, float] | None:
    try:
        bbox = tuple(float(v) for v in getattr(rect, "bbox"))
    except Exception:
        return None
    if len(bbox) != 4:
        return None
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return (bbox[0], bbox[1], bbox[2], bbox[3])


def _access_rect_trim_bboxes_for_obstacle(
    mutable_bbox: tuple[float, float, float, float],
    fixed_bbox: tuple[float, float, float, float],
    pdk: object,
    layer: str,
) -> tuple[tuple[tuple[float, float, float, float], str], ...]:
    x0, y0, x1, y1 = mutable_bbox
    fx0, fy0, fx1, fy1 = fixed_bbox
    grid = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 0.001)
    # The repair is evaluated before the later Calibre stream-grid snap.  Keep
    # more than one 5 nm stream-grid quantum so a repaired edge does not round
    # back into a touching same-layer contact.
    clearance = max(grid, 0.015)
    min_width = max(_min_width_um(pdk, layer, 0.05), grid)
    min_area = max(_min_area_um2(pdk, layer, 0.0), 0.0)
    raw = (
        ((fx1 + clearance, y0, x1, y1), "trim_left_edge", "y"),
        ((x0, y0, fx0 - clearance, y1), "trim_right_edge", "y"),
        ((x0, fy1 + clearance, x1, y1), "trim_bottom_edge", "x"),
        ((x0, y0, x1, fy0 - clearance), "trim_top_edge", "x"),
    )
    candidates: list[tuple[tuple[float, float, float, float], str]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for bbox, kind, extension_axis in raw:
        bx0, by0, bx1, by1 = bbox
        if bx1 - bx0 + 1e-12 < min_width or by1 - by0 + 1e-12 < min_width:
            continue
        candidate_bbox = (bx0, by0, bx1, by1)
        if min_area > 0.0 and (bx1 - bx0) * (by1 - by0) + 1e-12 < min_area:
            expanded = _extend_access_bbox_to_min_area_away_from_obstacle(
                candidate_bbox,
                fixed_bbox,
                min_area=min_area,
                axis=extension_axis,
            )
            if expanded is None:
                continue
            candidate_bbox = expanded
            bx0, by0, bx1, by1 = candidate_bbox
            kind = f"{kind}_with_min_area_extension"
        if _bbox_overlap_um(candidate_bbox, fixed_bbox):
            continue
        key = tuple(int(round(v * 1_000_000)) for v in (bx0, by0, bx1, by1))
        if key in seen:
            continue
        seen.add(key)
        candidates.append((candidate_bbox, kind))
    return tuple(candidates)


def _extend_access_bbox_to_min_area_away_from_obstacle(
    bbox: tuple[float, float, float, float],
    obstacle_bbox: tuple[float, float, float, float],
    *,
    min_area: float,
    axis: str,
) -> tuple[float, float, float, float] | None:
    x0, y0, x1, y1 = bbox
    width = x1 - x0
    height = y1 - y0
    if width <= 0.0 or height <= 0.0:
        return None
    if width * height + 1e-12 >= min_area:
        return bbox
    ox = (obstacle_bbox[0] + obstacle_bbox[2]) * 0.5
    oy = (obstacle_bbox[1] + obstacle_bbox[3]) * 0.5
    cx = (x0 + x1) * 0.5
    cy = (y0 + y1) * 0.5
    attempts: list[tuple[float, float, float, float]] = []
    if str(axis or "").lower() == "y":
        target_height = float(min_area) / max(width, 1e-12)
        delta = max(target_height - height, 0.0)
        if oy >= cy:
            attempts.extend(((x0, y0 - delta, x1, y1), (x0, y0, x1, y1 + delta)))
        else:
            attempts.extend(((x0, y0, x1, y1 + delta), (x0, y0 - delta, x1, y1)))
    elif str(axis or "").lower() == "x":
        target_width = float(min_area) / max(height, 1e-12)
        delta = max(target_width - width, 0.0)
        if ox >= cx:
            attempts.extend(((x0 - delta, y0, x1, y1), (x0, y0, x1 + delta, y1)))
        else:
            attempts.extend(((x0, y0, x1 + delta, y1), (x0 - delta, y0, x1, y1)))
    for candidate in attempts:
        if _bbox_overlap_um(candidate, obstacle_bbox):
            continue
        if (candidate[2] - candidate[0]) * (candidate[3] - candidate[1]) + 1e-12 >= min_area:
            return candidate
    return None


def _cluster_detour_path_candidates_for_obstacle(
    plan: object,
    shorts: Sequence[Mapping[str, object]],
    path_idx: int,
    pdk: object,
    *,
    max_jog_offset_um: float = 2.0,
) -> tuple[tuple[tuple[object, ...], dict[str, object]], ...]:
    """Replace a small connected same-net/layer path cluster with one detour.

    The single-path jog above is not enough when a local terminal escape is
    split across several path objects and the shorted endpoint must move with
    its neighbors.  This helper stays deliberately conservative: it only acts
    on one connected same-net/layer cluster with exactly two external endpoints.
    Candidate acceptance is still decided by the physical short precheck.
    """

    paths = tuple(getattr(plan, "paths", ()) or ())
    if path_idx < 0 or path_idx >= len(paths):
        return ()
    seed = paths[path_idx]
    net = str(getattr(seed, "net", "") or "")
    layer = str(getattr(seed, "layer", "") or "")
    if not net or not layer:
        return ()
    obstacle_bbox = _obstacle_bbox_for_path_rect_shorts(plan, shorts, path_idx)
    if obstacle_bbox is None:
        return ()
    same_layer_indices = tuple(
        idx
        for idx, path in enumerate(paths)
        if str(getattr(path, "net", "") or "") == net and str(getattr(path, "layer", "") or "") == layer
    )
    if len(same_layer_indices) < 2:
        return ()
    spacing = _rule_spacing_um(pdk, layer, 0.10)
    try:
        seed_width = max(float(getattr(seed, "width", 0.0) or 0.0), 0.0)
    except Exception:
        seed_width = 0.0
    membership_margin = max(seed_width + spacing + 0.12, 0.25)
    expanded_obstacle = _expand_bbox(obstacle_bbox, membership_margin)
    endpoint_to_indices: dict[tuple[int, int], set[int]] = {}
    path_points: dict[int, tuple[tuple[float, float], ...]] = {}
    path_bboxes: dict[int, tuple[float, float, float, float]] = {}
    for idx in same_layer_indices:
        points = _path_points_tuple(paths[idx])
        if len(points) < 2:
            continue
        path_points[idx] = points
        bbox = _path_bbox(paths[idx])
        if bbox is not None:
            path_bboxes[idx] = bbox
        for point in (points[0], points[-1]):
            endpoint_to_indices.setdefault(_point_grid_key(point), set()).add(idx)

    if path_idx not in path_points:
        return ()
    cluster = {path_idx}
    changed = True
    while changed and len(cluster) <= 8:
        changed = False
        connected = set(cluster)
        for idx in tuple(cluster):
            for point in (path_points[idx][0], path_points[idx][-1]):
                connected.update(endpoint_to_indices.get(_point_grid_key(point), set()))
        for idx in sorted(connected):
            if idx in cluster or idx not in path_bboxes:
                continue
            bbox = path_bboxes[idx]
            endpoint_inside = any(_point_inside_bbox(point, expanded_obstacle) for point in (path_points[idx][0], path_points[idx][-1]))
            if _bbox_overlap_um(bbox, expanded_obstacle) or endpoint_inside:
                cluster.add(idx)
                changed = True
    if len(cluster) < 2 or len(cluster) > 8:
        return ()

    boundary_points: dict[tuple[int, int], tuple[float, float]] = {}
    cluster_endpoint_counts: dict[tuple[int, int], int] = {}
    for idx in sorted(cluster):
        for point in (path_points[idx][0], path_points[idx][-1]):
            key = _point_grid_key(point)
            cluster_endpoint_counts[key] = cluster_endpoint_counts.get(key, 0) + 1
            boundary_points.setdefault(key, point)
    external_endpoint_keys: set[tuple[int, int]] = set()
    for key, point in boundary_points.items():
        connected_indices = endpoint_to_indices.get(key, set())
        if cluster_endpoint_counts.get(key, 0) == 1 or any(idx not in cluster for idx in connected_indices):
            external_endpoint_keys.add(key)
    external_points = tuple(boundary_points[key] for key in sorted(external_endpoint_keys))
    if len(external_points) != 2:
        return ()

    old_cluster_paths = tuple(paths[idx] for idx in sorted(cluster))
    widths = tuple(max(float(getattr(path, "width", 0.0) or 0.0), 0.0) for path in old_cluster_paths)
    detour_width = min((width for width in widths if width > 0.0), default=seed_width or 0.1)
    detour_paths = _manhattan_detour_polylines(
        external_points[0],
        external_points[1],
        obstacle_bbox,
        pdk,
        layer,
        width=detour_width,
        max_jog_offset_um=max_jog_offset_um,
    )
    if not detour_paths:
        return ()
    candidates: list[tuple[tuple[object, ...], dict[str, object]]] = []
    base_paths = tuple(path for idx, path in enumerate(paths) if idx not in cluster)
    prototype = seed
    for points in detour_paths:
        replacement_path = replace(prototype, points=points, width=detour_width)
        candidate_paths = (*base_paths, replacement_path)
        candidates.append(
            (
                tuple(candidate_paths),
                {
                    "kind": "same_net_layer_cluster_detour",
                    "seed_path_index": int(path_idx),
                    "removed_path_indices": tuple(int(idx) for idx in sorted(cluster)),
                    "net": net,
                    "layer": layer,
                    "obstacle_bbox": tuple(float(v) for v in obstacle_bbox),
                    "external_points": tuple(external_points),
                    "old_points_by_path": tuple(
                        {
                            "path_index": int(idx),
                            "points": tuple(getattr(paths[idx], "points", ()) or ()),
                            "width": float(getattr(paths[idx], "width", 0.0) or 0.0),
                        }
                        for idx in sorted(cluster)
                    ),
                    "new_points": tuple(points),
                    "new_width": float(detour_width),
                },
            )
        )
    return tuple(candidates)


def _manhattan_detour_polylines(
    start: tuple[float, float],
    end: tuple[float, float],
    obstacle_bbox: tuple[float, float, float, float],
    pdk: object,
    layer: str,
    *,
    width: float,
    max_jog_offset_um: float,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    spacing = _rule_spacing_um(pdk, layer, 0.10)
    clearance = max(float(width) + spacing + 0.08, 0.25)
    x_lo, y_lo, x_hi, y_hi = obstacle_bbox
    left_x = x_lo - clearance
    right_x = x_hi + clearance
    bottom_y = y_lo - clearance
    top_y = y_hi + clearance
    if max_jog_offset_um > 0.0:
        max_jog = max(float(max_jog_offset_um), clearance)
        left_x = max(left_x, start[0] - max_jog, end[0] - max_jog)
        right_x = min(right_x, start[0] + max_jog, end[0] + max_jog)
        bottom_y = max(bottom_y, start[1] - max_jog, end[1] - max_jog)
        top_y = min(top_y, start[1] + max_jog, end[1] + max_jog)
    raw_candidates = (
        (start, (left_x, start[1]), (left_x, end[1]), end),
        (start, (right_x, start[1]), (right_x, end[1]), end),
        (start, (start[0], bottom_y), (end[0], bottom_y), end),
        (start, (start[0], top_y), (end[0], top_y), end),
    )
    expanded = _expand_bbox(obstacle_bbox, max(width * 0.5, 0.0))
    candidates: list[tuple[tuple[float, float], ...]] = []
    seen: set[tuple[tuple[float, float], ...]] = set()
    for raw in raw_candidates:
        points = _dedupe_collinear_points(raw)
        if len(points) < 2:
            continue
        if _polyline_overlaps_bbox(points, width, expanded):
            continue
        key = tuple((round(point[0], 6), round(point[1], 6)) for point in points)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(points)
    return tuple(candidates)


def _dedupe_collinear_points(points: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    deduped: list[tuple[float, float]] = []
    for point in points:
        coerced = (float(point[0]), float(point[1]))
        if deduped and abs(deduped[-1][0] - coerced[0]) <= 1e-12 and abs(deduped[-1][1] - coerced[1]) <= 1e-12:
            continue
        deduped.append(coerced)
    changed = True
    while changed and len(deduped) >= 3:
        changed = False
        compacted: list[tuple[float, float]] = [deduped[0]]
        for idx in range(1, len(deduped) - 1):
            prev = compacted[-1]
            point = deduped[idx]
            nxt = deduped[idx + 1]
            if (
                abs(prev[0] - point[0]) <= 1e-12
                and abs(point[0] - nxt[0]) <= 1e-12
            ) or (
                abs(prev[1] - point[1]) <= 1e-12
                and abs(point[1] - nxt[1]) <= 1e-12
            ):
                changed = True
                continue
            compacted.append(point)
        compacted.append(deduped[-1])
        deduped = compacted
    return tuple(deduped)


def _polyline_overlaps_bbox(
    points: Sequence[tuple[float, float]],
    width: float,
    obstacle_bbox: tuple[float, float, float, float],
) -> bool:
    half = max(float(width), 0.0) * 0.5
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        segment_bbox = (
            min(x0, x1) - half,
            min(y0, y1) - half,
            max(x0, x1) + half,
            max(y0, y1) + half,
        )
        if _bbox_overlap_um(segment_bbox, obstacle_bbox):
            return True
    return False


def _path_points_tuple(path_obj: object) -> tuple[tuple[float, float], ...]:
    try:
        return tuple((float(point[0]), float(point[1])) for point in tuple(getattr(path_obj, "points", ()) or ()))
    except Exception:
        return ()


def _path_bbox(path_obj: object) -> tuple[float, float, float, float] | None:
    points = _path_points_tuple(path_obj)
    if len(points) < 2:
        return None
    try:
        width = max(float(getattr(path_obj, "width", 0.0) or 0.0), 0.0)
    except Exception:
        width = 0.0
    half = width * 0.5
    return (
        min(point[0] for point in points) - half,
        min(point[1] for point in points) - half,
        max(point[0] for point in points) + half,
        max(point[1] for point in points) + half,
    )


def _point_grid_key(point: tuple[float, float]) -> tuple[int, int]:
    return (int(round(float(point[0]) * 1_000_000)), int(round(float(point[1]) * 1_000_000)))


def _point_inside_bbox(point: tuple[float, float], bbox: tuple[float, float, float, float]) -> bool:
    return bbox[0] <= point[0] <= bbox[2] and bbox[1] <= point[1] <= bbox[3]


def _expand_bbox(bbox: tuple[float, float, float, float], margin: float) -> tuple[float, float, float, float]:
    margin = max(float(margin), 0.0)
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def _rule_spacing_um(pdk: object, layer: str, fallback: float) -> float:
    try:
        return max(float(pdk.rules.min_spacing_um(layer)), 0.0)
    except Exception:
        return max(float(fallback), 0.0)


def _apply_dsl_route_resources_to_route_specs(
    route_specs: Sequence[tuple[str, Sequence[str], str, float, int, str]],
    pdk: object,
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
) -> tuple[tuple[tuple[str, Sequence[str], str, float, int, str], ...], dict[str, dict[str, object]]]:
    resources = _dsl_route_resources(smt_result)
    if not resources:
        return (tuple(route_specs), {})
    updated: list[tuple[str, Sequence[str], str, float, int, str]] = []
    override_rows: dict[str, dict[str, object]] = {}
    for net, corridors, layer, width, lane, demand_name in route_specs:
        resource = _matching_route_resource(str(net), resources)
        if not resource:
            updated.append((net, corridors, layer, width, lane, demand_name))
            continue
        new_layer = _route_resource_layer(pdk, str(net), resource, str(layer))
        new_lane = _route_resource_lane(str(net), resource, int(lane))
        row = {
            "net": str(net),
            "match": resource.get("match", "net"),
            "resource_name": resource.get("name", ""),
            "layer_before": str(layer),
            "layer_after": str(new_layer),
            "lane_before": int(lane),
            "lane_after": int(new_lane),
            "allowed_layers": tuple(resource.get("allowed_layers", ()) or ()),
            "forbidden_layers": tuple(resource.get("forbidden_layers", ()) or ()),
            "cyclic_layers": tuple(resource.get("cyclic_layers", ()) or ()),
            "avoid_nets": tuple(resource.get("avoid_nets", ()) or ()),
            "avoid_prefixes": tuple(resource.get("avoid_prefixes", ()) or ()),
        }
        row.update(_route_resource_route_policy(resource))
        override_rows[str(net)] = row
        updated.append((net, corridors, new_layer, width, new_lane, demand_name))
    return (tuple(updated), override_rows)


def _dsl_route_resources(smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult) -> tuple[Mapping[str, object], ...]:
    checks = _mapping(getattr(smt_result, "checks", {}))
    return tuple(
        _mapping(item)
        for item in tuple(checks.get("dsl_route_resources", ()) or ())
        if isinstance(item, Mapping)
    )


def _matching_route_resource(net: str, resources: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    exact = [
        resource
        for resource in resources
        if str(resource.get("match", "net") or "net").lower() == "net" and str(resource.get("name", "") or "") == net
    ]
    if exact:
        return exact[-1]
    prefixes = [
        resource
        for resource in resources
        if str(resource.get("match", "") or "").lower() == "prefix"
        and net.startswith(str(resource.get("name", "") or ""))
    ]
    if not prefixes:
        return None
    return sorted(prefixes, key=lambda item: len(str(item.get("name", "") or "")), reverse=True)[0]


def _route_resource_layer(pdk: object, net: str, resource: Mapping[str, object], current_layer: str) -> str:
    forbidden = {str(item) for item in tuple(resource.get("forbidden_layers", ()) or ())}
    exact = _valid_route_layer(pdk, resource.get("layer"))
    if exact and exact not in forbidden:
        return exact
    cyclic = tuple(str(item) for item in tuple(resource.get("cyclic_layers", ()) or ()) if str(item))
    if cyclic:
        start = _numeric_suffix_index(net, default=0) % len(cyclic)
        for raw in cyclic[start:] + cyclic[:start]:
            layer = _valid_route_layer(pdk, raw)
            if layer and layer not in forbidden:
                return layer
    allowed = tuple(str(item) for item in tuple(resource.get("allowed_layers", ()) or ()) if str(item))
    if allowed:
        for raw in allowed:
            layer = _valid_route_layer(pdk, raw)
            if layer and layer not in forbidden:
                return layer
        return current_layer
    if current_layer in forbidden:
        return current_layer
    return current_layer


def _route_resource_lane(net: str, resource: Mapping[str, object], current_lane: int) -> int:
    raw_lane = resource.get("lane")
    if raw_lane is not None:
        try:
            return int(raw_lane)
        except (TypeError, ValueError):
            pass
    cyclic = tuple(resource.get("cyclic_lanes", ()) or ())
    if cyclic:
        raw_value = cyclic[_numeric_suffix_index(net, default=0) % len(cyclic)]
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            pass
    return int(current_lane)


def _route_resource_route_policy(resource: Mapping[str, object] | None) -> dict[str, object]:
    """Extract structured-route topology knobs from a DSL route resource row."""

    if not resource:
        return {}
    result: dict[str, object] = {}
    nested = resource.get("route_policy", resource.get("policy", {}))
    if isinstance(nested, Mapping):
        result.update({str(key): value for key, value in nested.items()})
    string_keys = (
        "style",
        "orientation",
        "channel_orientation",
        "trunk_orientation",
        "channel_side",
        "side",
        "dogleg_side",
        "terminal_escape_style",
        "escape_style",
    )
    for key in string_keys:
        value = resource.get(key)
        if value is not None and str(value):
            result[key] = str(value)
    numeric_keys = (
        "channel_offset_um",
        "channel_offset_nm",
        "dogleg_offset_um",
        "dogleg_offset_nm",
        "dogleg_offset_step_um",
        "dogleg_offset_step_nm",
        "terminal_escape_um",
        "terminal_escape_nm",
    )
    for key in numeric_keys:
        value = resource.get(key)
        if value is not None:
            result[key] = value
    bool_keys = ("prefer_horizontal", "prefer_vertical")
    for key in bool_keys:
        value = resource.get(key)
        if value is not None:
            result[key] = value
    return result


def _structured_routing_origin(smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult) -> str:
    return str(getattr(smt_result, "routing_origin", "hierarchical_smt_structured") or "hierarchical_smt_structured")


def _structured_rule_strategy(smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult) -> Mapping[str, object]:
    if isinstance(smt_result, AnalogFlatCompactSmtResult):
        strategy = smt_result.checks.get("rule_strategy", {})
        if isinstance(strategy, Mapping) and strategy:
            return strategy
        strategy = smt_result.base.problem.rule_metadata.get("rule_strategy", {})
        return strategy if isinstance(strategy, Mapping) else {}
    strategy = smt_result.problem.rule_metadata.get("rule_strategy", {})
    return strategy if isinstance(strategy, Mapping) else {}


def _structured_corridor_boxes_um(
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
) -> dict[str, tuple[float, float, float, float]]:
    if isinstance(smt_result, AnalogFlatCompactSmtResult):
        return {str(name): tuple(bbox) for name, bbox in smt_result.corridor_bboxes_um.items()}  # type: ignore[arg-type]
    return {
        name: _bbox_tracks_to_um(bbox, smt_result.track_pitch_um)
        for name, bbox in smt_result.physical.master.corridor_bboxes.items()
    }


def _structured_corridor_capacity_tracks(
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
) -> dict[str, int]:
    physical = smt_result.base.physical if isinstance(smt_result, AnalogFlatCompactSmtResult) else smt_result.physical
    return {str(name): int(value) for name, value in physical.master.corridor_capacity_tracks.items()}


def _structured_critical_load_by_corridor(
    smt_result: AnalogFlatCompactSmtResult | AnalogHierarchicalSmtResult,
) -> dict[str, int]:
    physical = smt_result.base.physical if isinstance(smt_result, AnalogFlatCompactSmtResult) else smt_result.physical
    return {str(name): int(value) for name, value in physical.master.critical_load_by_corridor.items()}


def _legal_route_width_um(pdk: object, layer: str, requested_width_um: float) -> float:
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "next_legal_width_um"):
        try:
            return float(rules.next_legal_width_um(str(layer), float(requested_width_um)))
        except Exception:
            pass
    if rules is not None and hasattr(rules, "snap_dimension_ceil_um"):
        try:
            return float(rules.snap_dimension_ceil_um(float(requested_width_um)))
        except Exception:
            pass
    return float(requested_width_um)


def _existing_net_or_pin_net(graph: TopologyGraph, *names: str) -> str:
    """Return a real graph net, accepting either net names or top-level pin names."""

    from analogskills.contracts import TerminalRef

    terminal_map = graph.terminal_net_map()
    for name in names:
        if str(name) in graph.nets:
            return str(name)
        pin_net = terminal_map.get(TerminalRef(str(name), "PIN"))
        if pin_net:
            return str(pin_net)
    return str(names[0]) if names else ""


def _route_layer(pdk: object, index: int) -> str:
    """Return a long-route metal from the PDK stack, skipping local M0 when present."""

    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    long_route_metals = tuple(layer for layer in metals if layer.upper() != "M0") or metals
    if not long_route_metals:
        long_route_metals = ("M1",)
    return long_route_metals[int(index) % len(long_route_metals)]


def _configured_route_layer(pdk: object, block: str, net: str, default_index: int) -> str:
    """Return a route trunk layer with optional config-driven promotion.

    The SMT resource model decides which corridors a net may use.  This helper
    decides which metal layer carries the structured trunk.  PDK/block config
    can promote sensitive nets away from local PCell access metals while the
    terminal-access generator still emits short local landings and via stacks.
    """

    default_layer = _route_layer(pdk, default_index)
    policy = _structured_route_layer_policy(pdk, block)
    exact_by_net = _mapping(policy.get("layer_by_net", {}))
    if str(net) in exact_by_net:
        exact = _valid_route_layer(pdk, exact_by_net[str(net)])
        if exact:
            return exact
    cyclic_by_prefix = _mapping(policy.get("cyclic_layer_by_net_prefix", policy.get("prefix_cyclic_layer_by_net", {})))
    for prefix, layer_names in sorted(cyclic_by_prefix.items(), key=lambda item: len(str(item[0])), reverse=True):
        if not str(net).startswith(str(prefix)):
            continue
        index = _numeric_suffix_index(str(net), default=0)
        sequence = tuple(str(item) for item in tuple(layer_names or ()))
        for raw_layer in sequence[index % max(1, len(sequence)) :] + sequence[: index % max(1, len(sequence))]:
            exact = _valid_route_layer(pdk, raw_layer)
            if exact:
                return exact
    prefix_by_net = _mapping(policy.get("prefix_layer_by_net", policy.get("layer_by_net_prefix", {})))
    for prefix, layer_name in sorted(prefix_by_net.items(), key=lambda item: len(str(item[0])), reverse=True):
        if str(net).startswith(str(prefix)):
            exact = _valid_route_layer(pdk, layer_name)
            if exact:
                return exact
    min_by_net = _mapping(policy.get("min_layer_by_net", {}))
    min_layer = _valid_route_layer(pdk, min_by_net.get(str(net)))
    if not min_layer:
        return default_layer
    return _higher_route_layer(pdk, default_layer, min_layer)


def _structured_route_layer_policy(pdk: object, block: str) -> Mapping[str, object]:
    rules = _block_smt_rules(pdk, block)
    routing = _mapping(rules.get("routing_resource", {}))
    return _mapping(routing.get("route_layers", {}))


def _configured_route_lane(pdk: object, block: str, net: str, default_lane: int) -> int:
    rules = _block_smt_rules(pdk, block)
    routing = _mapping(rules.get("routing_resource", {}))
    policy = _mapping(routing.get("route_lanes", {}))
    by_net = _mapping(policy.get("lane_by_net", {}))
    raw_lane = by_net.get(str(net))
    if raw_lane is None:
        cyclic_by_prefix = _mapping(policy.get("cyclic_lane_by_net_prefix", policy.get("prefix_cyclic_lane_by_net", {})))
        for prefix, lane_values in sorted(cyclic_by_prefix.items(), key=lambda item: len(str(item[0])), reverse=True):
            if not str(net).startswith(str(prefix)):
                continue
            sequence = tuple(lane_values or ())
            if sequence:
                raw_lane = sequence[_numeric_suffix_index(str(net), default=0) % len(sequence)]
                break
    if raw_lane is None:
        prefix_by_net = _mapping(policy.get("prefix_lane_by_net", policy.get("lane_by_net_prefix", {})))
        for prefix, lane_value in sorted(prefix_by_net.items(), key=lambda item: len(str(item[0])), reverse=True):
            if str(net).startswith(str(prefix)):
                raw_lane = lane_value
                break
    if raw_lane is None:
        return int(default_lane)
    try:
        return int(raw_lane)
    except (TypeError, ValueError):
        return int(default_lane)


def _numeric_suffix_index(name: str, *, default: int = 0) -> int:
    try:
        return max(0, int(str(name).rsplit("_", 1)[1]))
    except (IndexError, TypeError, ValueError):
        return int(default)


def _valid_route_layer(pdk: object, layer: object) -> str:
    name = str(layer or "")
    if not name:
        return ""
    metals = tuple(str(item) for item in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    if not metals:
        return name
    by_upper = {item.upper(): item for item in metals}
    return by_upper.get(name.upper(), "")


def _higher_route_layer(pdk: object, current: str, minimum: str) -> str:
    metals = tuple(str(item) for item in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    if current not in metals or minimum not in metals:
        return current
    return metals[max(metals.index(current), metals.index(minimum))]


def _collect_terminal_anchors_by_net(
    graph: TopologyGraph,
    pcell_plan: object,
    pdk: object,
    nets: Sequence[str],
    *,
    calibration_cache: object | None = None,
) -> dict[str, tuple[_StructuredTerminalAnchor, ...]]:
    from analogskills.pcell import PCellTerminalAccessor

    accessor = PCellTerminalAccessor(pdk, calibration_cache=calibration_cache)
    access_policy = _structured_terminal_access_policy(pdk)
    instance_by_name = {str(instance.name): instance for instance in getattr(pcell_plan, "instances", ())}
    unit_instances_by_parent = _pcell_unit_instances_by_parent(tuple(instance_by_name.values()))
    top_level_alias_by_net = _top_level_layout_alias_by_internal_net(graph)
    requested = set(str(net) for net in nets)
    anchors: dict[str, list[_StructuredTerminalAnchor]] = {net: [] for net in requested}
    for net in requested:
        if net not in graph.nets:
            continue
        accepted_connection_nets = {str(net)}
        alias = top_level_alias_by_net.get(str(net), "")
        if alias:
            accepted_connection_nets.add(alias)
        seen: set[tuple[str, str]] = set()
        for terminal in graph.nets[net].terminals:
            if terminal.device not in graph.devices:
                continue
            candidate_instances = _candidate_pcell_instances_for_terminal(
                instance_by_name,
                unit_instances_by_parent,
                str(terminal.device),
            )
            if not candidate_instances:
                continue
            for instance in candidate_instances:
                key = (str(getattr(instance, "name", "") or ""), str(terminal.terminal))
                if key in seen:
                    continue
                connections = dict(getattr(instance, "connections", {}) or {})
                if connections and str(connections.get(str(terminal.terminal), "")) not in {"", *accepted_connection_nets}:
                    continue
                seen.add(key)
                try:
                    pin = accessor.select_terminal_breakout(instance, terminal.terminal, require_lvs_safe=False)
                except Exception:
                    continue
                try:
                    xy = (float(pin.xy_um[0]), float(pin.xy_um[1]))
                except (AttributeError, TypeError, ValueError, IndexError):
                    continue
                layer = str(getattr(pin, "layer", "") or "")
                contact_layer = str(getattr(pin, "contact_layer", "") or "")
                crn28_anchor = _crn28_mos_calibrated_access_anchor(
                    pdk,
                    instance,
                    str(terminal.terminal),
                    xy,
                    access_policy,
                )
                if crn28_anchor is not None:
                    xy, layer, contact_layer = crn28_anchor
                anchors[net].append(
                    _StructuredTerminalAnchor(
                        xy,
                        layer,
                        contact_layer,
                        str(instance.name),
                        str(getattr(instance, "logical_name", "") or ""),
                        str(terminal.terminal),
                    )
                )
    return {
        net: tuple(sorted(points, key=lambda item: (item.xy_um, item.layer, item.contact_layer, item.instance, item.logical_name, item.terminal)))
        for net, points in anchors.items()
    }


def _top_level_layout_alias_by_internal_net(graph: TopologyGraph) -> dict[str, str]:
    try:
        terminal_map = graph.terminal_net_map()
    except Exception:
        return {}
    aliases: dict[str, str] = {}
    for pin in getattr(graph, "pins", {}) or {}:
        pin_name = str(pin)
        internal = str(terminal_map.get(TerminalRef(pin_name, "PIN"), pin_name))
        if internal and internal != pin_name:
            aliases[internal] = pin_name
    return aliases


def _pcell_unit_instances_by_parent(instances: Sequence[object]) -> dict[str, tuple[object, ...]]:
    grouped: dict[str, list[object]] = {}
    for instance in instances:
        name = str(getattr(instance, "name", "") or "")
        parent = _pcell_unit_parent_name(name)
        if not parent:
            continue
        grouped.setdefault(parent, []).append(instance)
    return {
        parent: tuple(sorted(items, key=lambda item: str(getattr(item, "name", "") or "")))
        for parent, items in grouped.items()
    }


def _pcell_unit_parent_name(instance_name: str) -> str:
    name = str(instance_name or "")
    if "_u" not in name:
        return ""
    parent, suffix = name.rsplit("_u", 1)
    if not parent or not suffix.isdigit():
        return ""
    return parent


def _candidate_pcell_instances_for_terminal(
    instance_by_name: Mapping[str, object],
    unit_instances_by_parent: Mapping[str, Sequence[object]],
    device_name: str,
) -> tuple[object, ...]:
    exact = instance_by_name.get(str(device_name))
    unit_instances = tuple(unit_instances_by_parent.get(str(device_name), ()) or ())
    if exact is None:
        return unit_instances
    return (exact, *tuple(instance for instance in unit_instances if instance is not exact))


def _compact_structured_unit_array_route_anchors(
    net: str,
    anchors: Sequence[_StructuredTerminalAnchor],
    pdk: object,
    *,
    route_layer: str,
    route_width_um: float,
    rect_factory: object,
) -> tuple[tuple[object, ...], tuple[_StructuredTerminalAnchor, ...], Mapping[str, object]]:
    """Locally bus unit-array anchors before connecting to a global trunk.

    Human analog layouts rarely route every primitive in a unit array directly
    to a high-level trunk.  They first short equivalent terminals locally using
    low metal, then expose one or a few taps to the global route.  This helper
    applies that policy only to expanded PCell unit arrays (``<parent>_uN``),
    keeping ordinary devices on the original direct-anchor path.
    """

    anchor_tuple = tuple(anchors)
    if len(anchor_tuple) < 2:
        return (), anchor_tuple, {"enabled": False, "reason": "fewer_than_two_anchors", "groups": ()}
    access_policy = _structured_terminal_access_policy(pdk)
    local_bus_cfg = _mapping(access_policy.get("unit_array_local_bus", {}))
    if not _bool_like(local_bus_cfg.get("enabled", False)):
        return (), anchor_tuple, {"enabled": False, "reason": "disabled_by_config", "groups": ()}
    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    if not metals:
        return (), anchor_tuple, {"enabled": False, "reason": "no_metal_layers", "groups": ()}
    metal_set = set(metals)
    groups: dict[tuple[str, str, str, str, str], list[_StructuredTerminalAnchor]] = {}
    passthrough: list[_StructuredTerminalAnchor] = []
    for anchor in anchor_tuple:
        parent = _pcell_unit_parent_name(str(anchor.instance))
        layer = str(anchor.layer or "")
        if not parent or layer not in metal_set:
            passthrough.append(anchor)
            continue
        key = (parent, str(anchor.terminal or ""), layer, str(anchor.contact_layer or ""), str(anchor.logical_name or ""))
        groups.setdefault(key, []).append(anchor)

    rects: list[object] = []
    routed_anchors: list[_StructuredTerminalAnchor] = list(passthrough)
    summaries: list[dict[str, object]] = []
    for (parent, terminal, layer, contact_layer, logical_name), group_items in sorted(groups.items()):
        ordered = tuple(sorted(group_items, key=lambda item: (item.xy_um[1], item.xy_um[0], item.instance)))
        if len(ordered) < 2:
            routed_anchors.extend(ordered)
            continue
        bus_rects, tap_anchors = _unit_array_local_bus_rects_for_anchor_group(
            net,
            ordered,
            pdk,
            layer=layer,
            route_layer=route_layer,
            route_width_um=route_width_um,
            rect_factory=rect_factory,
            config=local_bus_cfg,
        )
        if not bus_rects:
            routed_anchors.extend(ordered)
            continue
        rects.extend(bus_rects)
        routed_anchors.extend(tap_anchors)
        summaries.append(
            {
                "parent": parent,
                "terminal": terminal,
                "logical_name": logical_name,
                "layer": layer,
                "raw_anchor_count": len(ordered),
                "tap_instances": tuple(anchor.instance for anchor in tap_anchors),
                "tap_count": len(tap_anchors),
                "bus_rect_count": len(bus_rects),
            }
        )
    routed = tuple(sorted(routed_anchors, key=lambda item: (item.xy_um, item.layer, item.contact_layer, item.instance, item.logical_name, item.terminal)))
    return (
        tuple(rects),
        routed,
        {
            "enabled": True,
            "raw_anchor_count": len(anchor_tuple),
            "routed_anchor_count": len(routed),
            "local_bus_rect_count": len(rects),
            "groups": tuple(summaries),
        },
    )


def _unit_array_local_bus_rects_for_anchor_group(
    net: str,
    anchors: Sequence[_StructuredTerminalAnchor],
    pdk: object,
    *,
    layer: str,
    route_layer: str,
    route_width_um: float,
    rect_factory: object,
    config: Mapping[str, object],
) -> tuple[tuple[object, ...], tuple[_StructuredTerminalAnchor, ...]]:
    points = tuple(_structured_anchor_xy(anchor) for anchor in anchors)
    if len(points) < 2:
        return (), tuple(anchors)
    bus_layer = _unit_array_local_bus_layer(pdk, anchor_layer=layer, route_layer=route_layer, config=config)
    width_um = _dimension_cfg_um(
        config,
        "width_um",
        "width_nm",
        max(_min_width_um(pdk, bus_layer, 0.05), min(float(route_width_um), 0.16)),
    )
    width_um = max(float(width_um), _min_width_um(pdk, bus_layer, 0.05), 1e-6)
    half = 0.5 * width_um
    grid = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 1e-6)
    row_tol = max(_dimension_cfg_um(config, "row_tolerance_um", "row_tolerance_nm", max(width_um, grid * 2.0)), grid)
    col_tol = max(_dimension_cfg_um(config, "column_tolerance_um", "column_tolerance_nm", max(width_um, grid * 2.0)), grid)
    x_span = max(x for x, _y in points) - min(x for x, _y in points)
    y_span = max(y for _x, y in points) - min(y for _x, y in points)
    connect_rows = _bool_like(config.get("connect_rows", config.get("connect_array_rows", False)))
    connect_columns = _bool_like(config.get("connect_columns", config.get("connect_array_columns", False)))
    rects: list[object] = []
    tap_anchors: list[_StructuredTerminalAnchor] = []

    def add_bbox(raw_bbox: tuple[float, float, float, float]) -> None:
        rects.append(
            _rect_with_optional_metadata(
                rect_factory,
                str(bus_layer),
                "drawing",
                _snap_bbox(pdk, raw_bbox),
                str(net),
                {
                    "kind": "structured_unit_array_local_bus",
                    "net": str(net),
                    "anchor_layer": str(layer),
                    "route_layer": str(route_layer),
                    "bus_layer": str(bus_layer),
                    "anchor_count": len(anchors),
                },
            )
        )

    if x_span >= y_span:
        rows_with_anchors = _cluster_anchors_by_axis(anchors, axis=1, tolerance=row_tol)
        for row_anchors in rows_with_anchors:
            row = tuple(_structured_anchor_xy(anchor) for anchor in row_anchors)
            y = sum(point[1] for point in row) / max(len(row), 1)
            x0 = min(point[0] for point in row)
            x1 = max(point[0] for point in row)
            add_bbox((x0 - half, y - half, x1 + half, y + half))
            tap_anchors.append(_select_unit_array_local_bus_tap_anchor(row_anchors, route_layer=route_layer))
        if connect_rows and len(rows_with_anchors) > 1:
            tap_anchor = _select_unit_array_local_bus_tap_anchor(tap_anchors, route_layer=route_layer)
            tap_x, _tap_y = _structured_anchor_xy(tap_anchor)
            y0 = min(point[1] for point in points)
            y1 = max(point[1] for point in points)
            add_bbox((tap_x - half, y0 - half, tap_x + half, y1 + half))
            tap_anchors = [tap_anchor]
    else:
        cols_with_anchors = _cluster_anchors_by_axis(anchors, axis=0, tolerance=col_tol)
        for col_anchors in cols_with_anchors:
            col = tuple(_structured_anchor_xy(anchor) for anchor in col_anchors)
            x = sum(point[0] for point in col) / max(len(col), 1)
            y0 = min(point[1] for point in col)
            y1 = max(point[1] for point in col)
            add_bbox((x - half, y0 - half, x + half, y1 + half))
            tap_anchors.append(_select_unit_array_local_bus_tap_anchor(col_anchors, route_layer=route_layer))
        if connect_columns and len(cols_with_anchors) > 1:
            tap_anchor = _select_unit_array_local_bus_tap_anchor(tap_anchors, route_layer=route_layer)
            _tap_x, tap_y = _structured_anchor_xy(tap_anchor)
            x0 = min(point[0] for point in points)
            x1 = max(point[0] for point in points)
            add_bbox((x0 - half, tap_y - half, x1 + half, tap_y + half))
            tap_anchors = [tap_anchor]
    deduped: list[object] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    for rect in rects:
        bbox = tuple(float(v) for v in getattr(rect, "bbox"))
        key = (str(getattr(rect, "layer", "") or ""), *(int(round(value * 1_000_000)) for value in bbox))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rect)
    if bus_layer != layer:
        access_rects = _structured_access_rects_for_net(
            net,
            anchors,
            pdk,
            route_layer=bus_layer,
            route_width_um=width_um,
            rect_factory=rect_factory,
        )
        deduped = [*access_rects, *deduped]
    routed_taps = tuple(
        sorted(
            (replace(anchor, layer=bus_layer, contact_layer="") for anchor in tap_anchors),
            key=lambda item: (item.xy_um, item.layer, item.contact_layer, item.instance, item.logical_name, item.terminal),
        )
    )
    return tuple(deduped), routed_taps


def _rect_with_optional_metadata(
    rect_factory: object,
    layer: str,
    purpose: str,
    bbox: tuple[float, float, float, float],
    net: str,
    metadata: Mapping[str, object],
) -> object:
    meta = {str(key): value for key, value in dict(metadata or {}).items() if value not in ("", None)}
    try:
        return rect_factory(str(layer), str(purpose), bbox, str(net), metadata=meta)  # type: ignore[misc]
    except TypeError:
        try:
            return rect_factory(str(layer), str(purpose), bbox, str(net), "", meta)  # type: ignore[misc]
        except TypeError:
            return rect_factory(str(layer), str(purpose), bbox, str(net))  # type: ignore[misc]


def _unit_array_local_bus_layer(
    pdk: object,
    *,
    anchor_layer: str,
    route_layer: str,
    config: Mapping[str, object],
) -> str:
    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    if not metals:
        return str(anchor_layer)
    explicit = str(config.get("layer", config.get("bus_layer", "")) or "")
    if explicit in metals:
        return explicit
    anchor = str(anchor_layer)
    route = str(route_layer)
    if anchor not in metals or route not in metals:
        return anchor
    anchor_idx = metals.index(anchor)
    route_idx = metals.index(route)
    if route_idx <= anchor_idx:
        return route
    strategy = str(config.get("strategy", config.get("layer_strategy", "near_route")) or "near_route").strip().lower()
    if strategy in {"near_route", "below_route", "route_minus_one"}:
        offset = max(1, int(float(config.get("route_layer_offset", config.get("route_layer_offset_count", 1)) or 1)))
        bus_idx = max(anchor_idx + 1, route_idx - offset)
        return metals[min(route_idx, bus_idx)]
    default_layer = str(config.get("default_layer", config.get("default_bus_layer", "M4")) or "M4")
    default_idx = metals.index(default_layer) if default_layer in metals else min(anchor_idx + 2, route_idx)
    bus_idx = min(route_idx, max(anchor_idx + 1, default_idx))
    return metals[bus_idx]


def _select_unit_array_local_bus_tap_anchor(
    anchors: Sequence[_StructuredTerminalAnchor],
    *,
    route_layer: str,
) -> _StructuredTerminalAnchor:
    ordered = tuple(anchors)
    if not ordered:
        raise ValueError("at least one anchor is required")
    metals = ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10")
    try:
        route_idx = metals.index(str(route_layer))
    except ValueError:
        route_idx = len(metals) - 1
    # High-layer trunks are usually outside/above the dense array; choose an
    # edge tap to avoid a branch running through the middle of the array.
    if route_idx >= 6:
        return sorted(ordered, key=lambda item: (-item.xy_um[1], item.xy_um[0], item.instance))[0]
    return sorted(ordered, key=lambda item: (item.xy_um[1], item.xy_um[0], item.instance))[0]


def _cluster_points_by_axis(
    points: Sequence[tuple[float, float]],
    *,
    axis: int,
    tolerance: float,
) -> tuple[tuple[tuple[float, float], ...], ...]:
    if not points:
        return ()
    sorted_points = sorted(points, key=lambda point: (point[axis], point[1 - axis]))
    clusters: list[list[tuple[float, float]]] = []
    centers: list[float] = []
    for point in sorted_points:
        value = float(point[axis])
        if not clusters or abs(value - centers[-1]) > float(tolerance):
            clusters.append([point])
            centers.append(value)
            continue
        clusters[-1].append(point)
        centers[-1] = sum(item[axis] for item in clusters[-1]) / len(clusters[-1])
    return tuple(tuple(cluster) for cluster in clusters)


def _cluster_anchors_by_axis(
    anchors: Sequence[_StructuredTerminalAnchor],
    *,
    axis: int,
    tolerance: float,
) -> tuple[tuple[_StructuredTerminalAnchor, ...], ...]:
    if not anchors:
        return ()
    sorted_anchors = sorted(anchors, key=lambda anchor: (_structured_anchor_xy(anchor)[axis], _structured_anchor_xy(anchor)[1 - axis], anchor.instance))
    clusters: list[list[_StructuredTerminalAnchor]] = []
    centers: list[float] = []
    for anchor in sorted_anchors:
        value = float(_structured_anchor_xy(anchor)[axis])
        if not clusters or abs(value - centers[-1]) > float(tolerance):
            clusters.append([anchor])
            centers.append(value)
            continue
        clusters[-1].append(anchor)
        centers[-1] = sum(_structured_anchor_xy(item)[axis] for item in clusters[-1]) / len(clusters[-1])
    return tuple(tuple(cluster) for cluster in clusters)


def _structured_paths_for_net(
    net: str,
    anchors: Sequence[_StructuredTerminalAnchor],
    corridor_boxes_um: Sequence[tuple[float, float, float, float]],
    *,
    pdk: object | None = None,
    layer: str,
    width: float,
    lane: int,
    route_policy: Mapping[str, object] | None = None,
    path_factory: object,
) -> tuple[object, ...]:
    boxes = tuple(corridor_boxes_um)
    access_policy = _structured_terminal_access_policy(pdk) if pdk is not None else {}
    anchor_infos: list[
        tuple[
            _StructuredTerminalAnchor,
            tuple[float, float],
            tuple[float, float],
            float | None,
            tuple[tuple[tuple[float, float], tuple[float, float]], ...],
        ]
    ] = []
    span_points: list[tuple[float, float]] = []
    for anchor in anchors:
        xy = _structured_route_landing_xy_for_net_anchor(net, access_policy, anchor, layer, width, pdk)
        escaped_xy, escape_width, escape_segments = _structured_route_escape_segments_for_anchor(
            access_policy,
            anchor,
            layer,
            width,
            xy,
        )
        anchor_infos.append((anchor, xy, escaped_xy, escape_width, escape_segments))
        span_points.append(xy)
        span_points.append(escaped_xy)
    points = tuple(span_points)
    if not boxes and not points:
        return ()
    x0, y0, x1, y1 = _route_span_bbox(points, boxes)
    net_route_policy = dict(_structured_net_route_policy(pdk, net) if pdk is not None else {})
    net_route_policy.update(_route_resource_route_policy(route_policy))
    route_style = str(net_route_policy.get("style", "") or "").strip().lower()
    if _structured_route_style_is_dogleg(route_style) and len(anchor_infos) >= 2:
        dogleg_paths = _structured_dogleg_paths_for_net(
            net,
            anchor_infos,
            pdk=pdk,
            layer=layer,
            width=width,
            policy=net_route_policy,
            path_factory=path_factory,
        )
        if dogleg_paths:
            return dogleg_paths
    horizontal = _structured_route_prefers_horizontal(net_route_policy, default=(x1 - x0) >= (y1 - y0))
    lane_offset = float(lane) * max(width * 2.0, 0.2)
    paths: list[object] = []
    external_channel = _structured_route_style_is_external_channel(route_style)
    internal_channel = _structured_route_style_is_internal_channel(route_style)
    if internal_channel:
        trunk_x0, trunk_y0, trunk_x1, trunk_y1 = _route_span_bbox(points, boxes, margin_um=0.0)
        if not boxes and len(points) <= 1:
            # A reserved channel needs a concrete trunk even when a top-level
            # pin net has only one anchor and no explicit corridor.  Preserve
            # the pre-existing visible-route fallback for that degenerate case
            # while keeping multi-anchor/corridor reserved routes inside their
            # actual local envelope.
            trunk_x0, trunk_y0, trunk_x1, trunk_y1 = _route_span_bbox(
                points,
                boxes,
                margin_um=max(width * 2.0, 0.25),
            )
    else:
        trunk_x0, trunk_y0, trunk_x1, trunk_y1 = x0, y0, x1, y1
    if horizontal:
        if internal_channel:
            offset = max(
                _dimension_cfg_um(net_route_policy, "channel_offset_um", "channel_offset_nm", max(abs(lane_offset), width * 3.0, 0.2)),
                0.0,
            )
            side = str(net_route_policy.get("channel_side", net_route_policy.get("side", "center")) or "center").strip().lower()
            y = _inside_reserved_channel_coordinate(
                trunk_y0,
                trunk_y1,
                width=width,
                offset_um=offset,
                lane_offset_um=lane_offset,
                side=side,
                low_sides={"below", "bottom", "south", "s"},
                high_sides={"above", "top", "north", "n"},
            )
        elif external_channel:
            offset = max(
                _dimension_cfg_um(net_route_policy, "channel_offset_um", "channel_offset_nm", max(abs(lane_offset), width * 3.0, 0.2)),
                max(width * 3.0, 0.2),
            )
            side = str(net_route_policy.get("channel_side", net_route_policy.get("side", "above")) or "above").strip().lower()
            y = y0 - offset if side in {"below", "bottom", "south", "s"} else y1 + offset
        else:
            y = _clamp((_center_y(boxes[0]) if boxes else (y0 + y1) / 2.0) + lane_offset, y0, y1)
        paths.append(path_factory(layer, "drawing", ((trunk_x0, y), (trunk_x1, y)), width, net))
        for _anchor, (_ax, _ay), (ex, ey), escape_width, escape_segments in anchor_infos:
            _append_structured_route_escape_paths(paths, path_factory, layer, net, escape_width, escape_segments)
            if abs(ey - y) > 1e-9:
                paths.append(path_factory(layer, "drawing", ((ex, ey), (ex, y)), width, net))
    else:
        if internal_channel:
            offset = max(
                _dimension_cfg_um(net_route_policy, "channel_offset_um", "channel_offset_nm", max(abs(lane_offset), width * 3.0, 0.2)),
                0.0,
            )
            side = str(net_route_policy.get("channel_side", net_route_policy.get("side", "center")) or "center").strip().lower()
            x = _inside_reserved_channel_coordinate(
                trunk_x0,
                trunk_x1,
                width=width,
                offset_um=offset,
                lane_offset_um=lane_offset,
                side=side,
                low_sides={"left", "west", "w"},
                high_sides={"right", "east", "e"},
            )
        elif external_channel:
            offset = max(
                _dimension_cfg_um(net_route_policy, "channel_offset_um", "channel_offset_nm", max(abs(lane_offset), width * 3.0, 0.2)),
                max(width * 3.0, 0.2),
            )
            side = str(net_route_policy.get("channel_side", net_route_policy.get("side", "right")) or "right").strip().lower()
            x = x0 - offset if side in {"left", "west", "w"} else x1 + offset
        else:
            x = _clamp((_center_x(boxes[0]) if boxes else (x0 + x1) / 2.0) + lane_offset, x0, x1)
        paths.append(path_factory(layer, "drawing", ((x, trunk_y0), (x, trunk_y1)), width, net))
        for _anchor, (_ax, _ay), (ex, ey), escape_width, escape_segments in anchor_infos:
            _append_structured_route_escape_paths(paths, path_factory, layer, net, escape_width, escape_segments)
            if abs(ex - x) > 1e-9:
                paths.append(path_factory(layer, "drawing", ((ex, ey), (x, ey)), width, net))
    if not points:
        # Keep a small visible top-level shape so build_lvs_pins can bind a pin
        # to the net even before detailed terminal access is legalized.
        if horizontal:
            paths.append(path_factory(layer, "drawing", ((x0, y0), (min(x1, x0 + 0.5), y0)), width, net))
        else:
            paths.append(path_factory(layer, "drawing", ((x0, y0), (x0, min(y1, y0 + 0.5))), width, net))
    return tuple(paths)


def _structured_route_style_is_external_channel(route_style: str) -> bool:
    return str(route_style).strip().lower() in {
        "external_channel",
        "outside_channel",
        "external_trunk",
        "outside_trunk",
    }


def _structured_route_style_is_internal_channel(route_style: str) -> bool:
    return str(route_style).strip().lower() in {
        "internal_channel",
        "inside_channel",
        "reserved_channel",
        "internal_trunk",
        "inside_trunk",
        "reserved_trunk",
    }


def _structured_route_style_is_external_dogleg(route_style: str) -> bool:
    return str(route_style).strip().lower() in {"dogleg", "external_dogleg", "outside_dogleg"}


def _structured_route_style_is_internal_dogleg(route_style: str) -> bool:
    return str(route_style).strip().lower() in {
        "internal_dogleg",
        "inside_dogleg",
        "reserved_dogleg",
    }


def _structured_route_style_is_dogleg(route_style: str) -> bool:
    return _structured_route_style_is_external_dogleg(route_style) or _structured_route_style_is_internal_dogleg(route_style)


def _inside_reserved_channel_coordinate(
    lower: float,
    upper: float,
    *,
    width: float,
    offset_um: float,
    lane_offset_um: float,
    side: str,
    low_sides: set[str],
    high_sides: set[str],
) -> float:
    lo = min(float(lower), float(upper))
    hi = max(float(lower), float(upper))
    margin = max(float(width) * 0.5, 0.0)
    inner_lo = lo + margin
    inner_hi = hi - margin
    if inner_hi < inner_lo:
        return (lo + hi) * 0.5
    normalized_side = str(side).strip().lower()
    offset = max(float(offset_um), 0.0)
    if normalized_side in low_sides:
        target = inner_lo + offset
    elif normalized_side in high_sides:
        target = inner_hi - offset
    elif normalized_side in {"center", "middle", "mid", "c"}:
        target = (inner_lo + inner_hi) * 0.5
    else:
        target = (inner_lo + inner_hi) * 0.5 + float(lane_offset_um)
    return _clamp(target, inner_lo, inner_hi)


def _structured_route_prefers_horizontal(policy: Mapping[str, object], *, default: bool) -> bool:
    orientation = str(
        policy.get(
            "orientation",
            policy.get("channel_orientation", policy.get("trunk_orientation", "")),
        )
        or ""
    ).strip().lower()
    if orientation in {"horizontal", "h", "x"}:
        return True
    if orientation in {"vertical", "v", "y"}:
        return False
    if "prefer_horizontal" in policy:
        return _bool_like(policy.get("prefer_horizontal"))
    if "prefer_vertical" in policy:
        return not _bool_like(policy.get("prefer_vertical"))
    return bool(default)


def _structured_dogleg_paths_for_net(
    net: str,
    anchor_infos: Sequence[
        tuple[
            _StructuredTerminalAnchor,
            tuple[float, float],
            tuple[float, float],
            float | None,
            tuple[tuple[tuple[float, float], tuple[float, float]], ...],
        ]
    ],
    *,
    pdk: object | None,
    layer: str,
    width: float,
    policy: Mapping[str, object],
    path_factory: object,
) -> tuple[object, ...]:
    """Route dense two-terminal local nets through an external or reserved dogleg channel.

    This is intentionally a local ECO-style route, not a global SMT constraint:
    resistor-ladder series nodes should not run a straight trunk through the
    middle of neighboring resistor terminals just because their anchors are
    collinear.
    """

    if len(anchor_infos) < 2:
        return ()
    route_style = str(policy.get("style", "") or "").strip().lower()
    internal_dogleg = _structured_route_style_is_internal_dogleg(route_style)
    points = tuple(info[2] for info in anchor_infos)
    x0, y0, x1, y1 = _route_span_bbox(points, (), margin_um=0.0 if internal_dogleg else 0.25)
    offset_um = _dimension_cfg_um(policy, "dogleg_offset_um", "dogleg_offset_nm", 0.8)
    offset_step_um = _dimension_cfg_um(policy, "dogleg_offset_step_um", "dogleg_offset_step_nm", 0.0)
    index = _numeric_suffix_index(str(net), default=0)
    offset = max(float(offset_um) + float(offset_step_um) * float(index % 3), max(float(width) * 3.0, 0.2))
    terminal_escape_um = max(
        _dimension_cfg_um(
            policy,
            "terminal_escape_um",
            "terminal_escape_nm",
            0.0,
        ),
        0.0,
    )
    terminal_escape_style = str(
        policy.get("terminal_escape_style", policy.get("escape_style", ""))
        or ""
    ).strip().lower()
    outward_terminal_escape = terminal_escape_style in {"outward", "outside", "exterior", "away"}
    side = str(policy.get("dogleg_side", policy.get("side", "above")) or "above").strip().lower()
    if side in {"alternate", "alternating"}:
        side = "below" if index % 2 else "above"
    prefer_horizontal = _structured_route_prefers_horizontal(policy, default=(x1 - x0) >= (y1 - y0))
    paths: list[object] = []
    if prefer_horizontal:
        if internal_dogleg:
            channel_y = _inside_reserved_channel_coordinate(
                y0,
                y1,
                width=width,
                offset_um=offset,
                lane_offset_um=0.0,
                side=side,
                low_sides={"below", "bottom", "south", "s"},
                high_sides={"above", "top", "north", "n"},
            )
        elif side in {"below", "bottom", "south", "s"}:
            channel_y = y0 - offset
        else:
            channel_y = y1 + offset
        left_x = min(point[0] for point in points)
        right_x = max(point[0] for point in points)
        midpoint_x = (left_x + right_x) * 0.5
        escaped_points: list[tuple[float, float]] = []
        for _anchor, (_ax, _ay), (ex, ey), escape_width, escape_segments in anchor_infos:
            _append_structured_route_escape_paths(paths, path_factory, layer, net, escape_width, escape_segments)
            escape_x = ex
            if outward_terminal_escape and terminal_escape_um > 0.0:
                escape_x = ex - terminal_escape_um if ex <= midpoint_x else ex + terminal_escape_um
                if internal_dogleg:
                    escape_x = _clamp(escape_x, x0, x1)
                if abs(escape_x - ex) > 1e-9:
                    paths.append(path_factory(layer, "drawing", ((ex, ey), (escape_x, ey)), width, net))
            escaped_points.append((escape_x, ey))
        channel_left = min(point[0] for point in escaped_points) if escaped_points else left_x
        channel_right = max(point[0] for point in escaped_points) if escaped_points else right_x
        paths.append(path_factory(layer, "drawing", ((channel_left, channel_y), (channel_right, channel_y)), width, net))
        for escape_x, ey in escaped_points:
            if abs(ey - channel_y) > 1e-9:
                paths.append(path_factory(layer, "drawing", ((escape_x, ey), (escape_x, channel_y)), width, net))
        return tuple(paths)

    if internal_dogleg:
        channel_x = _inside_reserved_channel_coordinate(
            x0,
            x1,
            width=width,
            offset_um=offset,
            lane_offset_um=0.0,
            side=side,
            low_sides={"left", "west", "w"},
            high_sides={"right", "east", "e"},
        )
    elif side in {"left", "west", "w"}:
        channel_x = x0 - offset
    else:
        channel_x = x1 + offset
    bottom_y = min(point[1] for point in points)
    top_y = max(point[1] for point in points)
    midpoint_y = (bottom_y + top_y) * 0.5
    escaped_points = []
    for _anchor, (_ax, _ay), (ex, ey), escape_width, escape_segments in anchor_infos:
        _append_structured_route_escape_paths(paths, path_factory, layer, net, escape_width, escape_segments)
        escape_y = ey
        if outward_terminal_escape and terminal_escape_um > 0.0:
            escape_y = ey - terminal_escape_um if ey <= midpoint_y else ey + terminal_escape_um
            if internal_dogleg:
                escape_y = _clamp(escape_y, y0, y1)
            if abs(escape_y - ey) > 1e-9:
                paths.append(path_factory(layer, "drawing", ((ex, ey), (ex, escape_y)), width, net))
        escaped_points.append((ex, escape_y))
    channel_bottom = min(point[1] for point in escaped_points) if escaped_points else bottom_y
    channel_top = max(point[1] for point in escaped_points) if escaped_points else top_y
    paths.append(path_factory(layer, "drawing", ((channel_x, channel_bottom), (channel_x, channel_top)), width, net))
    for ex, escape_y in escaped_points:
        if abs(ex - channel_x) > 1e-9:
            paths.append(path_factory(layer, "drawing", ((ex, escape_y), (channel_x, escape_y)), width, net))
    return tuple(paths)


def _structured_net_route_policy(pdk: object | None, net: str) -> Mapping[str, object]:
    if pdk is None:
        return {}
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    structured = metadata.get("structured_interconnect", {})
    if not isinstance(structured, Mapping):
        return {}
    route_policies = structured.get("net_routes", structured.get("net_route_overrides", {}))
    if not isinstance(route_policies, Mapping):
        return {}
    by_net = _mapping(route_policies.get("by_net", route_policies.get("exact", {})))
    exact = by_net.get(str(net))
    if isinstance(exact, Mapping):
        return _mapping(exact)
    by_prefix = _mapping(route_policies.get("by_prefix", route_policies.get("prefix", {})))
    for prefix, value in sorted(by_prefix.items(), key=lambda item: len(str(item[0])), reverse=True):
        if str(net).startswith(str(prefix)) and isinstance(value, Mapping):
            return _mapping(value)
    return {}


def _structured_access_transition_escape(
    net: str,
    via: str,
    *,
    transition_escape_by_net: Mapping[str, object],
    transition_escape_by_via: Mapping[str, object],
    route_width_um: float,
) -> _StructuredTransitionEscape | None:
    net_cfg = _mapping(transition_escape_by_net.get(str(net), {}))
    raw = net_cfg.get(str(via))
    if not isinstance(raw, Mapping):
        raw = transition_escape_by_via.get(str(via))
    if not isinstance(raw, Mapping):
        return None
    cfg = _mapping(raw)
    dx_um = _dimension_cfg_um(cfg, "dx_um", "dx_nm", 0.0, allow_negative=True)
    dy_um = _dimension_cfg_um(cfg, "dy_um", "dy_nm", 0.0, allow_negative=True)
    if abs(dx_um) <= 1e-12 and abs(dy_um) <= 1e-12:
        return None
    width_um = _dimension_cfg_um(cfg, "width_um", "width_nm", float(route_width_um))
    merge_width_um = _dimension_cfg_um(
        cfg,
        "merge_width_um",
        "merge_width_nm",
        0.0,
    )
    return _StructuredTransitionEscape(
        dx_um=float(dx_um),
        dy_um=float(dy_um),
        width_um=max(float(width_um), 1e-6),
        merge_landing=_bool_like(cfg.get("merge_landing", cfg.get("merge_landings", False))),
        merge_width_um=max(float(merge_width_um), 0.0),
        merge_style=str(cfg.get("merge_style", cfg.get("merge_mode", "segment")) or "segment").strip().lower(),
    )


def _structured_route_escape_for_anchor(
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
    route_layer: str,
    route_width_um: float,
) -> tuple[float, float, float] | None:
    anchor_policy = _structured_anchor_terminal_access_policy(access_policy, anchor)
    raw = anchor_policy.get("route_escape", {})
    if not isinstance(raw, Mapping):
        return None
    layer = str(route_layer)
    exact_layer = str(raw.get("layer", "") or "")
    if exact_layer and exact_layer != layer:
        return None
    layers = tuple(str(item) for item in tuple(raw.get("layers", ()) or ()))
    if layers and layer not in layers:
        return None
    dx_um = _dimension_cfg_um(raw, "dx_um", "dx_nm", 0.0, allow_negative=True)
    dy_um = _dimension_cfg_um(raw, "dy_um", "dy_nm", 0.0, allow_negative=True)
    if abs(dx_um) <= 1e-12 and abs(dy_um) <= 1e-12:
        return None
    width_um = _dimension_cfg_um(raw, "width_um", "width_nm", float(route_width_um))
    return (float(dx_um), float(dy_um), max(float(width_um), 1e-6))


def _structured_route_escape_style_for_anchor(
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
    route_layer: str,
) -> str:
    anchor_policy = _structured_anchor_terminal_access_policy(access_policy, anchor)
    raw = anchor_policy.get("route_escape", {})
    if not isinstance(raw, Mapping):
        return ""
    layer = str(route_layer)
    exact_layer = str(raw.get("layer", "") or "")
    if exact_layer and exact_layer != layer:
        return ""
    layers = tuple(str(item) for item in tuple(raw.get("layers", ()) or ()))
    if layers and layer not in layers:
        return ""
    return str(
        raw.get(
            "style",
            raw.get("path_style", raw.get("mode", raw.get("escape_style", ""))),
        )
        or ""
    ).strip().lower()


def _structured_route_escape_partial_offset_for_anchor(
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
    route_layer: str,
    primary_key_um: str,
    primary_key_nm: str,
    default_um: float,
) -> float:
    anchor_policy = _structured_anchor_terminal_access_policy(access_policy, anchor)
    raw = anchor_policy.get("route_escape", {})
    if not isinstance(raw, Mapping):
        return float(default_um)
    layer = str(route_layer)
    exact_layer = str(raw.get("layer", "") or "")
    if exact_layer and exact_layer != layer:
        return float(default_um)
    layers = tuple(str(item) for item in tuple(raw.get("layers", ()) or ()))
    if layers and layer not in layers:
        return float(default_um)
    return float(_dimension_cfg_um(raw, primary_key_um, primary_key_nm, float(default_um), allow_negative=True))


def _structured_route_escape_segments_for_anchor(
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
    route_layer: str,
    route_width_um: float,
    start_xy: tuple[float, float],
) -> tuple[
    tuple[float, float],
    float | None,
    tuple[tuple[tuple[float, float], tuple[float, float]], ...],
]:
    escape = _structured_route_escape_for_anchor(access_policy, anchor, route_layer, route_width_um)
    if escape is None:
        return start_xy, None, ()
    dx_um, dy_um, width_um = escape
    start = (float(start_xy[0]), float(start_xy[1]))
    end = (start[0] + float(dx_um), start[1] + float(dy_um))
    style = _structured_route_escape_style_for_anchor(access_policy, anchor, route_layer)
    points: tuple[tuple[float, float], ...]
    if style in {"horizontal_first", "h_first", "x_first", "hv"}:
        points = (start, (start[0] + float(dx_um), start[1]), end)
    elif style in {"vertical_first", "v_first", "y_first", "vh"}:
        points = (start, (start[0], start[1] + float(dy_um)), end)
    elif style in {"horizontal_vertical_horizontal", "hvh"} and abs(dx_um) > 1e-12 and abs(dy_um) > 1e-12:
        first_dx = _structured_route_escape_partial_offset_for_anchor(
            access_policy,
            anchor,
            route_layer,
            "first_dx_um",
            "first_dx_nm",
            float(dx_um) * 0.5,
        )
        mid_x = start[0] + float(first_dx)
        points = (start, (mid_x, start[1]), (mid_x, start[1] + float(dy_um)), end)
    elif style in {"vertical_horizontal_vertical", "vhv"} and abs(dx_um) > 1e-12 and abs(dy_um) > 1e-12:
        first_dy = _structured_route_escape_partial_offset_for_anchor(
            access_policy,
            anchor,
            route_layer,
            "first_dy_um",
            "first_dy_nm",
            float(dy_um) * 0.5,
        )
        mid_y = start[1] + float(first_dy)
        points = (start, (start[0], mid_y), (start[0] + float(dx_um), mid_y), end)
    elif style in {"orthogonal", "manhattan", "dogleg"} and abs(dx_um) > 1e-12 and abs(dy_um) > 1e-12:
        points = (start, (start[0], start[1] + float(dy_um)), end)
    elif abs(dx_um) > 1e-12 and abs(dy_um) > 1e-12:
        # Physical route paths should stay Manhattan. If a config starts using
        # both dx and dy without an explicit style, prefer a deterministic
        # vertical-first dogleg over an implicit diagonal segment.
        points = (start, (start[0], start[1] + float(dy_um)), end)
    else:
        points = (start, end)
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    last: tuple[float, float] | None = None
    for point in points:
        if last is not None and (abs(last[0] - point[0]) > 1e-9 or abs(last[1] - point[1]) > 1e-9):
            segments.append((last, point))
        last = point
    return end, float(width_um), tuple(segments)


def _append_structured_route_escape_paths(
    paths: list[object],
    path_factory: object,
    layer: str,
    net: str,
    escape_width: float | None,
    escape_segments: Sequence[tuple[tuple[float, float], tuple[float, float]]],
) -> None:
    if escape_width is None:
        return
    for start_xy, end_xy in escape_segments:
        if abs(start_xy[0] - end_xy[0]) <= 1e-9 and abs(start_xy[1] - end_xy[1]) <= 1e-9:
            continue
        paths.append(path_factory(layer, "drawing", (start_xy, end_xy), escape_width, net))


def _structured_access_via_escape_for_anchor(
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
    route_layer: str,
    route_width_um: float,
) -> tuple[float, float, float] | None:
    anchor_policy = _structured_anchor_terminal_access_policy(access_policy, anchor)
    raw = anchor_policy.get("via_escape", anchor_policy.get("access_via_escape", {}))
    if not isinstance(raw, Mapping):
        return None
    layer = str(route_layer)
    exact_layer = str(raw.get("layer", "") or "")
    if exact_layer and exact_layer != layer:
        return None
    layers = tuple(str(item) for item in tuple(raw.get("layers", ()) or ()))
    if layers and layer not in layers:
        return None
    dx_um = _dimension_cfg_um(raw, "dx_um", "dx_nm", 0.0, allow_negative=True)
    dy_um = _dimension_cfg_um(raw, "dy_um", "dy_nm", 0.0, allow_negative=True)
    if abs(dx_um) <= 1e-12 and abs(dy_um) <= 1e-12:
        return None
    width_um = _dimension_cfg_um(raw, "width_um", "width_nm", float(route_width_um))
    return (float(dx_um), float(dy_um), max(float(width_um), 1e-6))


def _structured_route_landing_xy_for_anchor(
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
    route_layer: str,
    route_width_um: float,
    pdk: object | None,
) -> tuple[float, float]:
    """Return the final route-layer landing coordinate for an access stack.

    `_structured_access_rects_for_net` may move a via stack with configured
    transition escapes before reaching the selected route layer.  Routing paths
    must start from that final landing point; otherwise the path can miss the
    generated upper-metal landing and Calibre sees a same-net open.
    """

    x, y = _structured_anchor_xy(anchor)
    if pdk is None:
        return (x, y)
    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    if not metals:
        return (x, y)
    route_layer = str(route_layer)
    if route_layer not in metals:
        return (x, y)
    terminal_layer = str(anchor.layer or "")
    current_layer = terminal_layer
    gate_layer = str(getattr(getattr(pdk, "layer_map", None), "gate", "PO") or "PO")
    base_metal = metals[0]
    emit_gate_contacts = bool(access_policy.get("emit_gate_contacts", True))
    emit_unknown_nonmetal_contacts = bool(access_policy.get("emit_unknown_nonmetal_contacts", False))
    if terminal_layer == gate_layer:
        if not emit_gate_contacts:
            return (x, y)
        current_layer = base_metal
    elif terminal_layer not in metals:
        if not emit_unknown_nonmetal_contacts:
            return (x, y)
        current_layer = base_metal
    via_escape = _structured_access_via_escape_for_anchor(access_policy, anchor, route_layer, route_width_um)
    if via_escape is not None and terminal_layer in metals and terminal_layer != route_layer:
        x += via_escape[0]
        y += via_escape[1]
    if current_layer == route_layer:
        return (x, y)
    net = str(getattr(anchor, "net", "") or "")
    # `_StructuredTerminalAnchor` deliberately does not carry net to keep it
    # compact; callers pass transition policies by net in the access policy, so
    # the net-specific transition is handled by `_structured_route_landing_xy_for_net`.
    return (x, y)


def _structured_route_landing_xy_for_net_anchor(
    net: str,
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
    route_layer: str,
    route_width_um: float,
    pdk: object | None,
) -> tuple[float, float]:
    x, y = _structured_route_landing_xy_for_anchor(access_policy, anchor, route_layer, route_width_um, pdk)
    if pdk is None:
        return (x, y)
    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    if not metals:
        return (x, y)
    route_layer = str(route_layer)
    terminal_layer = str(anchor.layer or "")
    current_layer = terminal_layer
    gate_layer = str(getattr(getattr(pdk, "layer_map", None), "gate", "PO") or "PO")
    base_metal = metals[0]
    if terminal_layer == gate_layer:
        if not bool(access_policy.get("emit_gate_contacts", True)):
            return (x, y)
        current_layer = base_metal
    elif terminal_layer not in metals:
        if not bool(access_policy.get("emit_unknown_nonmetal_contacts", False)):
            return (x, y)
        current_layer = base_metal
    if current_layer == route_layer:
        return (x, y)
    transition_escape_by_net = _mapping(access_policy.get("transition_escape_by_net", {}))
    transition_escape_by_via = _mapping(access_policy.get("transition_escape_by_via", {}))
    for _lower, via, upper in _via_stack_between(pdk, current_layer, route_layer, metals):
        transition_escape = _structured_access_transition_escape(
            net,
            via,
            transition_escape_by_net=transition_escape_by_net,
            transition_escape_by_via=transition_escape_by_via,
            route_width_um=route_width_um,
        )
        if transition_escape is not None:
            x += transition_escape.dx_um
            y += transition_escape.dy_um
        if upper == route_layer:
            break
    return (x, y)


def _structured_access_rects_for_net(
    net: str,
    anchors: Sequence[_StructuredTerminalAnchor],
    pdk: object,
    *,
    route_layer: str,
    route_width_um: float,
    rect_factory: object,
) -> tuple[object, ...]:
    """Create conductive terminal landings and via stacks for structured routes.

    The hierarchical SMT router chooses long-route tracks, but PCell terminals
    are usually on M1 or PO.  Without these local access shapes Calibre sees the
    long route and the native PCell terminal as separate nets even when their
    coordinates match.
    """

    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    if not metals:
        return ()
    route_layer = str(route_layer)
    base_metal = metals[0]
    gate_layer = str(getattr(getattr(pdk, "layer_map", None), "gate", "PO") or "PO")
    default_contact = str(getattr(getattr(pdk, "layer_map", None), "contact", "CO") or "CO")
    access_policy = _structured_terminal_access_policy(pdk)
    emit_gate_contacts = bool(access_policy.get("emit_gate_contacts", True))
    emit_unknown_nonmetal_contacts = bool(access_policy.get("emit_unknown_nonmetal_contacts", False))
    landing_uses_route_width = bool(access_policy.get("landing_uses_route_width", False))
    min_landing_half = max(float(access_policy.get("min_landing_half_um", 0.0) or 0.0), 0.0)
    redundant_via_min_width = max(float(access_policy.get("redundant_via_min_adjacent_width_um", 0.0) or 0.0), 0.0)
    redundant_via_cuts = max(int(access_policy.get("redundant_via_cuts", 1) or 1), 1)
    redundant_via_cuts_by_via = _mapping(access_policy.get("redundant_via_cuts_by_via", {}))
    force_redundant_via_by_via = {
        str(key): _bool_like(value)
        for key, value in _mapping(access_policy.get("force_redundant_via_by_via", {})).items()
    }
    transition_escape_by_net = _mapping(access_policy.get("transition_escape_by_net", {}))
    transition_escape_by_via = _mapping(access_policy.get("transition_escape_by_via", {}))
    force_redundant_via_by_net = _mapping(access_policy.get("force_redundant_via_by_net", {}))
    redundant_via_cuts_by_net = _mapping(access_policy.get("redundant_via_cuts_by_net", {}))
    redundant_via_axis = str(access_policy.get("redundant_via_axis", "x") or "x").lower()
    redundant_via_axis_by_via = {
        str(key): str(value or "").strip().lower()
        for key, value in _mapping(access_policy.get("redundant_via_axis_by_via", {})).items()
        if str(value or "").strip()
    }
    landing_axis = str(access_policy.get("landing_axis", redundant_via_axis) or redundant_via_axis).lower()
    rects: list[object] = []
    seen: set[tuple[str, int, int, int, int]] = set()
    active_access_metadata: dict[str, object] = {"kind": "structured_terminal_access", "net": str(net), "route_layer": str(route_layer)}
    anchor_by_instance_terminal: dict[tuple[str, str, str], _StructuredTerminalAnchor] = {}
    for candidate in anchors:
        instance = str(getattr(candidate, "instance", "") or "")
        terminal = str(getattr(candidate, "terminal", "") or "")
        if not instance or not terminal:
            continue
        logical = str(getattr(candidate, "logical_name", "") or "")
        for terminal_key in {terminal, terminal.lower(), terminal.upper()}:
            anchor_by_instance_terminal.setdefault((logical, instance, terminal_key), candidate)

    def add_bbox(layer: str, raw_bbox: tuple[float, float, float, float]) -> None:
        bbox = _snap_exact_size_bbox_around_center(pdk, raw_bbox) if _is_cut_layer(pdk, layer) else _snap_bbox(pdk, raw_bbox)
        key = (str(layer), *(int(round(v * 1000_000)) for v in bbox))
        if key in seen:
            return
        seen.add(key)
        rects.append(
            _rect_with_optional_metadata(
                rect_factory,
                str(layer),
                "drawing",
                bbox,
                net,
                {**active_access_metadata, "shape_layer": str(layer)},
            )
        )

    def add_box(
        layer: str,
        x_half: float,
        y_half: float,
        x: float,
        y: float,
        *,
        landing_direction: str = "center",
        near_x: float = 0.0,
        near_y: float = 0.0,
    ) -> None:
        if x_half <= 0.0 or y_half <= 0.0:
            return
        raw_bbox = _landing_bbox_around_anchor(
            x,
            y,
            x_half,
            y_half,
            landing_direction=landing_direction,
            near_x=max(float(near_x), 0.0),
            near_y=max(float(near_y), 0.0),
        )
        add_bbox(layer, raw_bbox)

    def add_rect(layer: str, half: float, x: float, y: float) -> None:
        add_box(layer, half, half, x, y)

    def add_segment_box(layer: str, width_um: float, x0: float, y0: float, x1: float, y1: float) -> None:
        half = max(float(width_um) * 0.5, _min_width_um(pdk, layer, 0.05) * 0.5)
        if abs(x1 - x0) <= 1e-12 and abs(y1 - y0) <= 1e-12:
            return
        if abs(y1 - y0) <= 1e-12:
            add_bbox(layer, (min(x0, x1) - half, y0 - half, max(x0, x1) + half, y0 + half))
            return
        if abs(x1 - x0) <= 1e-12:
            add_bbox(layer, (x0 - half, min(y0, y1) - half, x0 + half, max(y0, y1) + half))
            return
        add_bbox(layer, (min(x0, x1) - half, y0 - half, max(x0, x1) + half, y0 + half))
        add_bbox(layer, (x1 - half, min(y0, y1) - half, x1 + half, max(y0, y1) + half))

    def metal_landing_size(layer: str, *, include_route_width: bool = False, axis: str | None = None) -> tuple[float, float]:
        landing_axis_local = str(axis or landing_axis)
        min_half = max(_min_width_um(pdk, layer, 0.05) * 0.5, min_landing_half)
        min_area = _min_area_um2(pdk, layer, 0.0)
        if landing_axis_local in {"y", "vertical", "v"}:
            x_half = min_half
            y_half = max(min_half, min_area / max(4.0 * x_half, 1e-12))
        elif landing_axis_local in {"x", "horizontal", "h"}:
            y_half = min_half
            x_half = max(min_half, min_area / max(4.0 * y_half, 1e-12))
        else:
            half = max(min_half, _min_area_square_half_um(pdk, layer, 0.0))
            x_half = y_half = half
        if include_route_width and landing_uses_route_width:
            if landing_axis_local in {"y", "vertical", "v"}:
                x_half = max(x_half, float(route_width_um) * 0.5)
            elif landing_axis_local in {"x", "horizontal", "h"}:
                y_half = max(y_half, float(route_width_um) * 0.5)
            else:
                x_half = max(x_half, float(route_width_um) * 0.5)
                y_half = max(y_half, float(route_width_um) * 0.5)
        return (x_half, y_half)

    for anchor in anchors:
        terminal_layer = str(anchor.layer or "")
        active_access_metadata = {
            "kind": "structured_terminal_access",
            "net": str(net),
            "route_layer": str(route_layer),
            "instance": str(getattr(anchor, "instance", "") or ""),
            "logical_name": str(getattr(anchor, "logical_name", "") or ""),
            "terminal": str(getattr(anchor, "terminal", "") or ""),
            "terminal_layer": terminal_layer,
            "contact_layer": str(getattr(anchor, "contact_layer", "") or ""),
        }
        x, y = _structured_anchor_xy(anchor)
        anchor_policy = _structured_anchor_terminal_access_policy(access_policy, anchor)
        anchor_redundant_via_cuts = _positive_int(anchor_policy.get("redundant_via_cuts", redundant_via_cuts), redundant_via_cuts)
        anchor_redundant_via_cuts_by_via = {
            **redundant_via_cuts_by_via,
            **{
                str(key): _positive_int(value, 1)
                for key, value in _mapping(
                    anchor_policy.get("redundant_via_cuts_by_via", anchor_policy.get("redundant_via_cut_count_by_via", {}))
                ).items()
            },
        }
        anchor_force_redundant_via_by_via = {
            **force_redundant_via_by_via,
            **{
                str(key): _bool_like(value)
                for key, value in _mapping(
                    anchor_policy.get("force_redundant_via_by_via", anchor_policy.get("redundant_via_force_by_via", {}))
                ).items()
            },
        }
        anchor_redundant_via_axis = str(anchor_policy.get("redundant_via_axis", redundant_via_axis) or redundant_via_axis).lower()
        anchor_redundant_via_axis_by_via = {
            **redundant_via_axis_by_via,
            **{
                str(key): str(value or "").strip().lower()
                for key, value in _mapping(
                    anchor_policy.get("redundant_via_axis_by_via", anchor_policy.get("redundant_via_cut_axis_by_via", {}))
                ).items()
                if str(value or "").strip()
            },
        }
        anchor_landing_axis = str(anchor_policy.get("landing_axis", landing_axis) or landing_axis)
        anchor_landing_direction = str(anchor_policy.get("landing_direction", access_policy.get("landing_direction", "center")) or "center")
        anchor_directional_landing_overlap_um = _dimension_cfg_um(
            anchor_policy,
            "directional_landing_overlap_um",
            "directional_landing_overlap_nm",
            _dimension_cfg_um(
                access_policy,
                "directional_landing_overlap_um",
                "directional_landing_overlap_nm",
                0.0,
            ),
        )
        directional_layers = tuple(str(item) for item in tuple(anchor_policy.get("directional_landing_layers", ()) or ()))
        existing_landing_layers = tuple(str(item) for item in tuple(anchor_policy.get("existing_landing_layers", ()) or ()))

        def direction_for_layer(layer: str) -> str:
            if not directional_layers or str(layer) in directional_layers:
                return anchor_landing_direction
            return "center"

        def emit_landing_for_layer(layer: str) -> bool:
            return str(layer) not in existing_landing_layers

        current_layer = terminal_layer
        shared_stack_terminal = str(
            anchor_policy.get(
                "share_via_stack_with_terminal",
                anchor_policy.get("shared_via_stack_terminal", ""),
            )
            or ""
        ).strip()
        if shared_stack_terminal and terminal_layer in metals:
            peer_anchor = None
            logical = str(getattr(anchor, "logical_name", "") or "")
            instance = str(getattr(anchor, "instance", "") or "")
            for terminal_key in (shared_stack_terminal, shared_stack_terminal.lower(), shared_stack_terminal.upper()):
                peer_anchor = anchor_by_instance_terminal.get((logical, instance, terminal_key))
                if peer_anchor is not None:
                    break
            if peer_anchor is not None and peer_anchor is not anchor and str(peer_anchor.layer or "") == terminal_layer:
                peer_x, peer_y = _structured_anchor_xy(peer_anchor)
                if emit_landing_for_layer(terminal_layer):
                    add_box(
                        terminal_layer,
                        *metal_landing_size(terminal_layer, axis=anchor_landing_axis),
                        x,
                        y,
                        landing_direction=direction_for_layer(terminal_layer),
                    )
                bridge_width_um = _dimension_cfg_um(
                    anchor_policy,
                    "shared_stack_bridge_width_um",
                    "shared_stack_bridge_width_nm",
                    float(route_width_um),
                )
                add_segment_box(terminal_layer, bridge_width_um, x, y, peer_x, peer_y)
                continue

        via_escape = _structured_access_via_escape_for_anchor(access_policy, anchor, route_layer, route_width_um)
        if via_escape is not None and terminal_layer in metals and terminal_layer != route_layer:
            dx_um, dy_um, escape_width_um = via_escape
            escaped_x = x + dx_um
            escaped_y = y + dy_um
            if emit_landing_for_layer(terminal_layer):
                add_box(
                    terminal_layer,
                    *metal_landing_size(terminal_layer, axis=anchor_landing_axis),
                    x,
                    y,
                    landing_direction=direction_for_layer(terminal_layer),
                )
            add_segment_box(terminal_layer, escape_width_um, x, y, escaped_x, escaped_y)
            x = escaped_x
            y = escaped_y

        if terminal_layer == gate_layer:
            if not emit_gate_contacts:
                continue
            contact = str(anchor.contact_layer or default_contact)
            contact_half = max(_min_width_um(pdk, contact, 0.04) * 0.5, 0.02)
            add_rect(contact, contact_half, x, y)
            add_rect(gate_layer, max(contact_half + _enclosure_um(pdk, contact, gate_layer, 0.045), 0.07), x, y)
            base_xh, base_yh = metal_landing_size(base_metal, axis=anchor_landing_axis)
            add_rect(
                base_metal,
                max(contact_half + _enclosure_um(pdk, contact, base_metal, 0.025), base_xh, base_yh),
                x,
                y,
            )
            current_layer = base_metal
        elif terminal_layer not in metals:
            if not emit_unknown_nonmetal_contacts:
                continue
            contact = str(anchor.contact_layer or default_contact)
            contact_half = max(_min_width_um(pdk, contact, 0.04) * 0.5, 0.02)
            add_rect(contact, contact_half, x, y)
            base_xh, base_yh = metal_landing_size(base_metal, axis=anchor_landing_axis)
            base_enc = contact_half + _enclosure_um(pdk, contact, base_metal, 0.025)
            add_box(
                base_metal,
                max(base_xh, base_enc),
                max(base_yh, base_enc),
                x,
                y,
                landing_direction=direction_for_layer(base_metal),
                near_x=base_enc,
                near_y=base_enc,
            )
            current_layer = base_metal
        else:
            # A path on the same metal layer already physically touches the
            # terminal coordinate.  Do not add an extra same-layer pad: for
            # dense analog PCells that pad can short adjacent terminal breakouts.
            if current_layer != route_layer and emit_landing_for_layer(current_layer):
                add_box(
                    current_layer,
                    *metal_landing_size(current_layer, axis=anchor_landing_axis),
                    x,
                    y,
                    landing_direction=direction_for_layer(current_layer),
                )

        if current_layer == route_layer:
            continue
        for lower, via, upper in _via_stack_between(pdk, current_layer, route_layer, metals):
            transition_escape = _structured_access_transition_escape(
                net,
                via,
                transition_escape_by_net=transition_escape_by_net,
                transition_escape_by_via=transition_escape_by_via,
                route_width_um=route_width_um,
            )
            if transition_escape is not None:
                dx_um = transition_escape.dx_um
                dy_um = transition_escape.dy_um
                escape_width_um = transition_escape.width_um
                escaped_x = x + dx_um
                escaped_y = y + dy_um
                lower_escape_width_um = escape_width_um
                if transition_escape.merge_landing:
                    lower_escape_width_um = max(
                        lower_escape_width_um,
                        transition_escape.merge_width_um,
                    )
                if transition_escape.merge_landing and transition_escape.merge_style in {"gap", "gap_fill", "notch_fill"}:
                    add_segment_box(lower, escape_width_um, x, y, escaped_x, escaped_y)
                    merge_width_um = max(
                        transition_escape.merge_width_um,
                        escape_width_um,
                        _min_width_um(pdk, lower, 0.05),
                    )
                    old_xh, old_yh = metal_landing_size(lower, axis=anchor_landing_axis)
                    merge_half = merge_width_um * 0.5
                    overlap_um = max(float(getattr(pdk, "grid_um", 0.001) or 0.001), 0.005)
                    min_parallel_half = _min_width_um(pdk, lower, 0.05) * 0.5
                    if abs(dy_um) <= 1e-12 and abs(dx_um) > 1e-12:
                        if dx_um > 0:
                            gap_a = x + old_xh
                            gap_b = escaped_x - merge_half
                        else:
                            gap_a = escaped_x + merge_half
                            gap_b = x - old_xh
                        center_x = (gap_a + gap_b) * 0.5
                        half_x = max(min_parallel_half, abs(gap_b - gap_a) * 0.5 + overlap_um)
                        add_bbox(lower, (center_x - half_x, y - merge_half, center_x + half_x, y + merge_half))
                    elif abs(dx_um) <= 1e-12 and abs(dy_um) > 1e-12:
                        if dy_um > 0:
                            gap_a = y + old_yh
                            gap_b = escaped_y - merge_half
                        else:
                            gap_a = escaped_y + merge_half
                            gap_b = y - old_yh
                        center_y = (gap_a + gap_b) * 0.5
                        half_y = max(min_parallel_half, abs(gap_b - gap_a) * 0.5 + overlap_um)
                        add_bbox(lower, (x - merge_half, center_y - half_y, x + merge_half, center_y + half_y))
                    else:
                        add_segment_box(lower, lower_escape_width_um, x, y, escaped_x, escaped_y)
                else:
                    add_segment_box(lower, lower_escape_width_um, x, y, escaped_x, escaped_y)
                if upper == route_layer:
                    add_segment_box(upper, max(escape_width_um, float(route_width_um)), x, y, escaped_x, escaped_y)
                x = escaped_x
                y = escaped_y
            via_half = max(_min_width_um(pdk, via, 0.05) * 0.5, 0.025)
            lower_xh, lower_yh = metal_landing_size(lower, axis=anchor_landing_axis)
            upper_xh, upper_yh = metal_landing_size(upper, include_route_width=(upper == route_layer), axis=anchor_landing_axis)
            lower_enc = _enclosure_um(pdk, via, lower, 0.025)
            upper_enc = _enclosure_um(pdk, via, upper, 0.025)
            lower_xh = max(lower_xh, via_half + lower_enc)
            lower_yh = max(lower_yh, via_half + lower_enc)
            upper_xh = max(upper_xh, via_half + upper_enc)
            upper_yh = max(upper_yh, via_half + upper_enc)
            adjacent_width = max(
                lower_xh * 2.0,
                lower_yh * 2.0,
                upper_xh * 2.0,
                upper_yh * 2.0,
                float(route_width_um) if upper == route_layer or lower == route_layer else 0.0,
            )
            via_redundant_cuts = _positive_int(anchor_redundant_via_cuts_by_via.get(via, anchor_redundant_via_cuts), anchor_redundant_via_cuts)
            via_redundant_cuts = _net_via_positive_int(
                redundant_via_cuts_by_net,
                net,
                via,
                via_redundant_cuts,
            )
            force_redundant_via = bool(
                anchor_force_redundant_via_by_via.get(via, False)
                or _net_via_bool(force_redundant_via_by_net, net, via)
            )
            cut_offsets = _structured_via_cut_offsets(
                pdk,
                via,
                via_half=via_half,
                cut_count=via_redundant_cuts if force_redundant_via or (redundant_via_min_width and adjacent_width > redundant_via_min_width) else 1,
                axis=anchor_redundant_via_axis_by_via.get(via, anchor_redundant_via_axis),
                direction=direction_for_layer(lower),
            )
            if cut_offsets:
                max_cut_x = max(abs(dx) + via_half for dx, _dy in cut_offsets)
                max_cut_y = max(abs(dy) + via_half for _dx, dy in cut_offsets)
                lower_xh = max(lower_xh, max_cut_x + lower_enc)
                lower_yh = max(lower_yh, max_cut_y + lower_enc)
                upper_xh = max(upper_xh, max_cut_x + upper_enc)
                upper_yh = max(upper_yh, max_cut_y + upper_enc)
            lower_near_x, lower_near_y = _landing_near_extent_for_direction(
                direction_for_layer(lower),
                cut_offsets or ((0.0, 0.0),),
                via_half,
                lower_enc,
                extra_overlap=anchor_directional_landing_overlap_um,
            )
            upper_near_x, upper_near_y = _landing_near_extent_for_direction(
                direction_for_layer(upper),
                cut_offsets or ((0.0, 0.0),),
                via_half,
                upper_enc,
                extra_overlap=anchor_directional_landing_overlap_um,
            )
            if emit_landing_for_layer(lower):
                add_box(
                    lower,
                    lower_xh,
                    lower_yh,
                    x,
                    y,
                    landing_direction=direction_for_layer(lower),
                    near_x=lower_near_x,
                    near_y=lower_near_y,
                )
            for dx, dy in cut_offsets or ((0.0, 0.0),):
                add_rect(via, via_half, x + dx, y + dy)
            if emit_landing_for_layer(upper):
                add_box(
                    upper,
                    upper_xh,
                    upper_yh,
                    x,
                    y,
                    landing_direction=direction_for_layer(upper),
                    near_x=upper_near_x,
                    near_y=upper_near_y,
                )
    return tuple(rects)


def _landing_bbox_around_anchor(
    x: float,
    y: float,
    x_half: float,
    y_half: float,
    *,
    landing_direction: str = "center",
    near_x: float = 0.0,
    near_y: float = 0.0,
) -> tuple[float, float, float, float]:
    direction = str(landing_direction or "center").strip().lower()
    if direction in {"left", "west", "w"}:
        right = max(float(near_x), 0.0)
        total = max(2.0 * float(x_half), right)
        return (float(x) - (total - right), float(y) - float(y_half), float(x) + right, float(y) + float(y_half))
    if direction in {"right", "east", "e"}:
        left = max(float(near_x), 0.0)
        total = max(2.0 * float(x_half), left)
        return (float(x) - left, float(y) - float(y_half), float(x) + (total - left), float(y) + float(y_half))
    if direction in {"down", "bottom", "south", "s"}:
        top = max(float(near_y), 0.0)
        total = max(2.0 * float(y_half), top)
        return (float(x) - float(x_half), float(y) - (total - top), float(x) + float(x_half), float(y) + top)
    if direction in {"up", "top", "north", "n"}:
        bottom = max(float(near_y), 0.0)
        total = max(2.0 * float(y_half), bottom)
        return (float(x) - float(x_half), float(y) - bottom, float(x) + float(x_half), float(y) + (total - bottom))
    return (float(x) - float(x_half), float(y) - float(y_half), float(x) + float(x_half), float(y) + float(y_half))


def _landing_near_extent_for_direction(
    landing_direction: str,
    cut_offsets: Sequence[tuple[float, float]],
    via_half: float,
    enclosure: float,
    *,
    extra_overlap: float = 0.0,
) -> tuple[float, float]:
    direction = str(landing_direction or "center").strip().lower()
    offsets = tuple(cut_offsets) or ((0.0, 0.0),)
    overlap = max(float(extra_overlap), 0.0)
    if direction in {"left", "west", "w"}:
        return (max(float(dx) + float(via_half) for dx, _dy in offsets) + float(enclosure) + overlap, 0.0)
    if direction in {"right", "east", "e"}:
        return (max(-float(dx) + float(via_half) for dx, _dy in offsets) + float(enclosure) + overlap, 0.0)
    if direction in {"down", "bottom", "south", "s"}:
        return (0.0, max(float(dy) + float(via_half) for _dx, dy in offsets) + float(enclosure) + overlap)
    if direction in {"up", "top", "north", "n"}:
        return (0.0, max(-float(dy) + float(via_half) for _dx, dy in offsets) + float(enclosure) + overlap)
    return (0.0, 0.0)


def _structured_anchor_terminal_access_policy(
    access_policy: Mapping[str, object],
    anchor: _StructuredTerminalAnchor,
) -> Mapping[str, object]:
    overrides = _mapping(access_policy.get("terminal_overrides", {}))
    logical = str(getattr(anchor, "logical_name", "") or "")
    terminal = str(getattr(anchor, "terminal", "") or "")
    instance = str(getattr(anchor, "instance", "") or "")
    parent_instance = _pcell_unit_parent_name(instance)
    keys = (
        f"{instance}.{terminal}" if instance and terminal else "",
        f"{instance}.{terminal.lower()}" if instance and terminal else "",
        f"{parent_instance}.{terminal}" if parent_instance and terminal else "",
        f"{parent_instance}.{terminal.lower()}" if parent_instance and terminal else "",
        f"{logical}.{terminal}" if logical and terminal else "",
        f"{logical}.{terminal.lower()}" if logical and terminal else "",
        terminal,
        terminal.lower(),
    )
    for key in keys:
        if not key:
            continue
        value = overrides.get(key)
        if isinstance(value, Mapping):
            return _mapping(value)
    nested = overrides.get(logical)
    if isinstance(nested, Mapping):
        for key in (terminal, terminal.lower()):
            value = nested.get(key)
            if isinstance(value, Mapping):
                return _mapping(value)
    return {}


def _structured_terminal_access_policy(pdk: object) -> dict[str, object]:
    """Return configurable terminal-access knobs for SMT structured routing."""

    metadata = getattr(pdk, "metadata", {}) or {}
    raw: object = {}
    if isinstance(metadata, Mapping):
        structured = metadata.get("structured_interconnect", {})
        if isinstance(structured, Mapping):
            raw = structured.get("terminal_access", {})
        if not isinstance(raw, Mapping) or not raw:
            smt_rules = metadata.get("smt_design_rules", {})
            if isinstance(smt_rules, Mapping):
                structured = smt_rules.get("structured_interconnect", {})
                if isinstance(structured, Mapping):
                    raw = structured.get("terminal_access", {})
    cfg = raw if isinstance(raw, Mapping) else {}

    def bool_cfg(name: str, default: bool) -> bool:
        value = cfg.get(name, default)
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no"}
        return bool(value)

    min_half = cfg.get("min_landing_half_um", None)
    if min_half is None and "min_landing_half_nm" in cfg:
        try:
            min_half = float(cfg["min_landing_half_nm"]) * 1e-3
        except (TypeError, ValueError):
            min_half = None
    try:
        min_landing_half_um = float(min_half) if min_half is not None else 0.0
    except (TypeError, ValueError):
        min_landing_half_um = 0.0
    return {
        "emit_gate_contacts": bool_cfg("emit_gate_contacts", True),
        "emit_unknown_nonmetal_contacts": bool_cfg("emit_unknown_nonmetal_contacts", False),
        "landing_uses_route_width": bool_cfg("landing_uses_route_width", False),
        "min_landing_half_um": max(min_landing_half_um, 0.0),
        "redundant_via_min_adjacent_width_um": _dimension_cfg_um(
            cfg,
            "redundant_via_min_adjacent_width_um",
            "redundant_via_min_adjacent_width_nm",
            0.0,
        ),
        "redundant_via_cuts": _positive_int(cfg.get("redundant_via_cuts", 1), 1),
        "redundant_via_cuts_by_via": {
            str(key): _positive_int(value, 1)
            for key, value in _mapping(
                cfg.get("redundant_via_cuts_by_via", cfg.get("redundant_via_cut_count_by_via", {}))
            ).items()
        },
        "redundant_via_max_pitch_um_by_via": _dimension_mapping_cfg_um(
            cfg,
            "redundant_via_max_pitch_um_by_via",
            "redundant_via_max_pitch_nm_by_via",
        ),
        "force_redundant_via_by_via": {
            str(key): _bool_like(value)
            for key, value in _mapping(
                cfg.get("force_redundant_via_by_via", cfg.get("redundant_via_force_by_via", {}))
            ).items()
        },
        "force_redundant_via_by_net": _mapping(
            cfg.get("force_redundant_via_by_net", cfg.get("redundant_via_force_by_net", {}))
        ),
        "redundant_via_cuts_by_net": _mapping(
            cfg.get("redundant_via_cuts_by_net", cfg.get("redundant_via_cut_count_by_net", {}))
        ),
        "transition_escape_by_net": _mapping(cfg.get("transition_escape_by_net", {})),
        "transition_escape_by_via": _mapping(cfg.get("transition_escape_by_via", {})),
        "redundant_via_axis": str(cfg.get("redundant_via_axis", "x") or "x"),
        "redundant_via_axis_by_via": {
            str(key): str(value or "")
            for key, value in _mapping(
                cfg.get("redundant_via_axis_by_via", cfg.get("redundant_via_cut_axis_by_via", {}))
            ).items()
        },
        "landing_axis": str(cfg.get("landing_axis", cfg.get("redundant_via_axis", "x")) or "x"),
        "landing_direction": str(cfg.get("landing_direction", "center") or "center"),
        "terminal_overrides": _mapping(cfg.get("terminal_overrides", {})),
        "unit_array_local_bus": _mapping(cfg.get("unit_array_local_bus", cfg.get("mos_unit_array_local_bus", {}))),
        "use_crn28_mos_gate_access_anchor": bool_cfg("use_crn28_mos_gate_access_anchor", False),
        "use_crn28_mos_calibrated_access_anchor": bool_cfg(
            "use_crn28_mos_calibrated_access_anchor",
            bool_cfg("use_crn28_mos_gate_access_anchor", False),
        ),
        "crn28_mos_gate_bus_y_offset_um": _dimension_cfg_um(
            cfg,
            "crn28_mos_gate_bus_y_offset_um",
            "crn28_mos_gate_bus_y_offset_nm",
            -0.140,
            allow_negative=True,
        ),
    }


def _use_crn28_mos_calibrated_access_anchor(
    pdk: object,
    instance: object,
    terminal: str,
    access_policy: Mapping[str, object],
) -> bool:
    if not bool(access_policy.get("use_crn28_mos_calibrated_access_anchor", False)):
        return False
    if str(getattr(pdk, "name", "")).lower() != "crn28hpcp":
        return False
    if str(terminal).upper() not in {"S", "D", "G", "B"}:
        return False
    return str(getattr(instance, "logical_name", "") or "").lower() in {"nmos", "pmos"}


def _crn28_mos_calibrated_access_anchor(
    pdk: object,
    instance: object,
    terminal: str,
    raw_xy: tuple[float, float],
    access_policy: Mapping[str, object],
) -> tuple[tuple[float, float], str, str] | None:
    """Project CRN28 MOS route anchors onto generated Calibre-safe access.

    ``build_crn28_mos_multifinger_access_plan`` creates explicit S/D M2 buses,
    gate M1 contacts, and a body tap.  Structured routing should land on those
    helper shapes instead of native PCell terminal centers; otherwise route via
    stacks can be dropped inside dense native terminal geometry and short
    adjacent nets during LVS/DRC.
    """

    if not _use_crn28_mos_calibrated_access_anchor(pdk, instance, terminal, access_policy):
        return None
    term = str(terminal).upper()
    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    m1 = metals[0] if metals else "M1"
    m2 = metals[min(1, len(metals) - 1)] if metals else "M2"
    if term == "G":
        return (_crn28_mos_gate_access_anchor_xy(instance, raw_xy, access_policy), m1, "")
    if term in {"S", "D"}:
        point = _crn28_mos_sd_bus_anchor_xy(pdk, instance, term)
        if point is None:
            return None
        return (point, m2, "")
    if term == "B":
        point = _crn28_mos_body_tap_anchor_xy(pdk, instance)
        if point is None:
            return None
        return (point, m1, "")
    return None


def _crn28_mos_gate_access_anchor_xy(
    instance: object,
    raw_xy: tuple[float, float],
    access_policy: Mapping[str, object],
) -> tuple[float, float]:
    origin = tuple(getattr(instance, "xy_um", (0.0, 0.0)) or (0.0, 0.0))
    if len(origin) < 2:
        return raw_xy
    offset = float(access_policy.get("crn28_mos_gate_bus_y_offset_um", -0.140) or -0.140)
    orient = str(getattr(instance, "orient", "R0") or "R0").upper()
    raw_x, raw_y = float(raw_xy[0]), float(raw_xy[1])
    ox, oy = float(origin[0]), float(origin[1])
    landing_centers = _crn28_mos_gate_landing_centers_um(instance, offset)
    if landing_centers:
        target = min(
            landing_centers,
            key=lambda point: (point[0] - raw_x) ** 2 + (point[1] - raw_y) ** 2,
        )
        return target
    if orient in {"R0", "MY"}:
        return (raw_x, oy + offset)
    if orient in {"R180", "MX"}:
        return (raw_x, oy - offset)
    if orient in {"R90", "MXR90"}:
        return (ox - offset, raw_y)
    if orient in {"R270", "MYR90"}:
        return (ox + offset, raw_y)
    return (raw_x, oy + offset)


def _crn28_mos_gate_landing_centers_um(instance: object, gate_bus_offset_um: float) -> tuple[tuple[float, float], ...]:
    """Return calibrated CRN28 MOS gate M1 landing centers for route access.

    The native PCell gate pin coordinate can sit near the gate-bus edge.  Using
    that coordinate for a vertical route stack can overlap nearby source/body
    stacks on VIA layers and create non-rectangular merged cut polygons.  The
    calibrated access generator emits one M1 landing per gate finger; route
    access should land on those centers instead.
    """

    params = dict(getattr(instance, "params", {}) or {})
    try:
        nf = max(1, int(float(params.get("fingers", params.get("nf", 1)) or 1)))
    except (TypeError, ValueError):
        nf = 1
    try:
        sim_m = max(1, int(float(params.get("simM", params.get("m", params.get("M", 1))) or 1)))
    except (TypeError, ValueError):
        sim_m = 1
    count = max(1, nf * sim_m)
    length_um = _crn28_instance_param_um(params, ("l", "L", "length"), 0.12)
    pitch_um = max(0.24, length_um + 0.12)
    origin = tuple(getattr(instance, "xy_um", (0.0, 0.0)) or (0.0, 0.0))
    if len(origin) < 2:
        return ()
    orient = str(getattr(instance, "orient", "R0") or "R0").upper()
    local_y = float(gate_bus_offset_um)
    centers: list[tuple[float, float]] = []
    for idx in range(count):
        local_x = idx * pitch_um + 0.5 * length_um
        centers.append(_oriented_point_um((float(origin[0]), float(origin[1])), (local_x, local_y), orient))
    return tuple(centers)


def _crn28_mos_sd_bus_anchor_xy(pdk: object, instance: object, terminal: str) -> tuple[float, float] | None:
    geometry = _crn28_mos_access_geometry_um(pdk, instance)
    if geometry is None:
        return None
    local_x = geometry["center_x"]
    local_y = geometry["source_bus_y"] if str(terminal).upper() == "S" else geometry["drain_bus_y"]
    origin = geometry["origin"]
    orient = geometry["orient"]
    point = _oriented_point_um(origin, (local_x, local_y), orient)
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "snap_point_um"):
        try:
            return tuple(rules.snap_point_um(point))  # type: ignore[return-value]
        except Exception:
            pass
    return point


def _crn28_mos_body_tap_anchor_xy(pdk: object, instance: object) -> tuple[float, float] | None:
    geometry = _crn28_mos_access_geometry_um(pdk, instance)
    if geometry is None:
        return None
    point = _oriented_point_um(geometry["origin"], (geometry["body_tap_x"], geometry["body_tap_y"]), geometry["orient"])
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "snap_point_um"):
        try:
            return tuple(rules.snap_point_um(point))  # type: ignore[return-value]
        except Exception:
            pass
    return point


def _crn28_mos_access_geometry_um(pdk: object, instance: object) -> dict[str, object] | None:
    params = dict(getattr(instance, "params", {}) or {})
    try:
        nf = max(1, int(float(params.get("fingers", params.get("nf", 1)) or 1)))
    except (TypeError, ValueError):
        nf = 1
    try:
        sim_m = max(1, int(float(params.get("simM", params.get("m", params.get("M", 1))) or 1)))
    except (TypeError, ValueError):
        sim_m = 1
    origin_raw = tuple(getattr(instance, "xy_um", (0.0, 0.0)) or (0.0, 0.0))
    if len(origin_raw) < 2:
        return None
    length_um = _crn28_instance_param_um(params, ("l", "L", "length"), 0.12)
    width_um = _crn28_instance_param_um(params, ("Wfg",), 0.0)
    if width_um <= 0.0:
        total_width_um = _crn28_instance_param_um(params, ("W", "w", "width"), 1.0)
        width_um = total_width_um / max(nf * sim_m, 1)
    pitch_um = max(0.24, length_um + 0.12)
    column_count = nf * sim_m + 1
    min_x = -0.06
    max_x = -0.06 + float(column_count - 1) * pitch_um
    active_top = max(width_um, 0.2)
    source_bus_y = _snap_scalar_um(pdk, active_top + 0.16)
    drain_bus_y = _snap_scalar_um(pdk, active_top + 0.54)
    body_tap_y = _snap_scalar_um(pdk, -1.18)
    center_x = _snap_scalar_um(pdk, (min_x + max_x) * 0.5)
    body_tap_x = _crn28_mos_body_tap_x_um(pdk, min_x, max_x)
    return {
        "origin": (float(origin_raw[0]), float(origin_raw[1])),
        "orient": str(getattr(instance, "orient", "R0") or "R0").upper(),
        "center_x": center_x,
        "body_tap_x": body_tap_x,
        "source_bus_y": source_bus_y,
        "drain_bus_y": drain_bus_y,
        "body_tap_y": body_tap_y,
    }


def _crn28_mos_body_tap_x_um(pdk: object, min_x: float, max_x: float) -> float:
    access_cfg = _crn28_mos_access_config(pdk)
    mode = str(access_cfg.get("body_tap_x_mode", "center") or "center").strip().lower()
    margin = max(float(access_cfg.get("body_tap_side_margin_um", 0.62) or 0.62), 0.0)
    if mode in {"left", "start", "outside_left"}:
        return _snap_scalar_um(pdk, float(min_x) - margin)
    if mode in {"right", "end", "outside_right"}:
        return _snap_scalar_um(pdk, float(max_x) + margin)
    return _snap_scalar_um(pdk, (float(min_x) + float(max_x)) * 0.5)


def _crn28_mos_access_config(pdk: object) -> Mapping[str, object]:
    metadata = getattr(pdk, "metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return {}
    calibre = metadata.get("calibre", {})
    if not isinstance(calibre, Mapping):
        return {}
    raw = calibre.get("mos_access", {})
    if not isinstance(raw, Mapping):
        return {}

    def dimension_um(nm_key: str, default_um: float) -> float:
        value = raw.get(nm_key, raw.get(nm_key.replace("_nm", ""), None))
        try:
            return max(float(value) * 1e-3, 0.0) if value is not None else float(default_um)
        except (TypeError, ValueError):
            return float(default_um)

    return {
        "body_tap_x_mode": str(raw.get("body_tap_x_mode", "center") or "center"),
        "body_tap_side_margin_um": dimension_um("body_tap_side_margin_nm", 0.62),
    }


def _snap_scalar_um(pdk: object, value: float) -> float:
    rules = getattr(pdk, "rules", None)
    if rules is not None and hasattr(rules, "snap_um"):
        try:
            return float(rules.snap_um(float(value)))
        except Exception:
            pass
    return float(value)


def _crn28_instance_param_um(params: Mapping[str, object], keys: Sequence[str], default_um: float) -> float:
    for key in keys:
        if key not in params:
            continue
        value = params.get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        # PCell parameters are generally SI meters.  Test fixtures may pass
        # already-normalized microns; keep values >= 10 nm as microns.
        if abs(number) < 1e-5:
            return number * 1e6
        return number
    return float(default_um)


def _oriented_point_um(
    origin: tuple[float, float],
    local: tuple[float, float],
    orient: str,
) -> tuple[float, float]:
    x, y = local
    if orient == "R0":
        dx, dy = x, y
    elif orient == "R90":
        dx, dy = -y, x
    elif orient == "R180":
        dx, dy = -x, -y
    elif orient == "R270":
        dx, dy = y, -x
    elif orient == "MX":
        dx, dy = x, -y
    elif orient == "MY":
        dx, dy = -x, y
    elif orient == "MXR90":
        dx, dy = y, x
    elif orient == "MYR90":
        dx, dy = -y, -x
    else:
        dx, dy = x, y
    return (origin[0] + dx, origin[1] + dy)


def _base_metal_layer(pdk: object) -> str:
    metals = tuple(str(layer) for layer in getattr(getattr(pdk, "layer_map", None), "metals", ()) or ())
    return metals[0] if metals else "M1"


def _dimension_cfg_um(
    cfg: Mapping[str, object],
    um_key: str,
    nm_key: str,
    default_um: float,
    *,
    allow_negative: bool = False,
) -> float:
    value = cfg.get(um_key, None)
    if value is None and nm_key in cfg:
        try:
            value = float(cfg[nm_key]) * 1e-3
        except (TypeError, ValueError):
            value = None
    try:
        number = float(value) if value is not None else float(default_um)
    except (TypeError, ValueError):
        number = float(default_um)
    return number if allow_negative else max(number, 0.0)


def _dimension_mapping_cfg_um(
    cfg: Mapping[str, object],
    um_key: str,
    nm_key: str,
) -> dict[str, float]:
    """Return a per-name dimension map in um, accepting either um or nm config."""

    result: dict[str, float] = {}
    for key, value in _mapping(cfg.get(nm_key, {})).items():
        try:
            result[str(key)] = max(float(value) * 1e-3, 0.0)
        except (TypeError, ValueError):
            continue
    for key, value in _mapping(cfg.get(um_key, {})).items():
        try:
            result[str(key)] = max(float(value), 0.0)
        except (TypeError, ValueError):
            continue
    return result


def _int_cfg(
    cfg: Mapping[str, object],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        value = int(cfg.get(key, default))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(value, int(minimum))
    if maximum is not None:
        value = min(value, int(maximum))
    return value


def _structured_anchor_xy(anchor: _StructuredTerminalAnchor | tuple[float, float]) -> tuple[float, float]:
    if isinstance(anchor, _StructuredTerminalAnchor):
        return (float(anchor.xy_um[0]), float(anchor.xy_um[1]))
    return (float(anchor[0]), float(anchor[1]))


def _via_stack_between(
    pdk: object,
    start_layer: str,
    end_layer: str,
    metals: Sequence[str],
) -> tuple[tuple[str, str, str], ...]:
    if start_layer not in metals or end_layer not in metals:
        return ()
    start = metals.index(start_layer)
    end = metals.index(end_layer)
    if start == end:
        return ()
    step = 1 if end > start else -1
    result: list[tuple[str, str, str]] = []
    for idx in range(start, end, step):
        lower = metals[idx]
        upper = metals[idx + step]
        via_rule = getattr(pdk, "via_rule_for_layers", lambda _a, _b: None)(lower, upper)
        via = str(getattr(via_rule, "via_def", "") or "")
        if not via:
            return ()
        result.append((lower, via, upper))
    return tuple(result)


def _structured_via_cut_offsets(
    pdk: object,
    via: str,
    *,
    via_half: float,
    cut_count: int,
    axis: str = "x",
    direction: str = "center",
) -> tuple[tuple[float, float], ...]:
    cuts = max(int(cut_count), 1)
    if cuts <= 1:
        return ((0.0, 0.0),)
    spacing = _via_array_spacing_um(pdk, via, 0.08)
    pitch = max(float(via_half) * 2.0 + spacing, float(via_half) * 2.0)
    access_policy = _structured_terminal_access_policy(pdk)
    max_pitch_by_via = _mapping(access_policy.get("redundant_via_max_pitch_um_by_via", {}))
    try:
        max_pitch = float(max_pitch_by_via.get(str(via), 0.0) or 0.0)
    except (TypeError, ValueError):
        max_pitch = 0.0
    if max_pitch > 0.0:
        pitch = min(pitch, max(max_pitch, float(via_half) * 2.0))
    if cuts == 2:
        array_direction = str(direction or "center").strip().lower()
        if str(axis).lower() in {"y", "vertical", "v"}:
            if array_direction in {"down", "bottom", "south", "s"}:
                return ((0.0, 0.0), (0.0, -pitch))
            return ((0.0, 0.0), (0.0, pitch))
        if array_direction in {"left", "west", "w"}:
            return ((0.0, 0.0), (-pitch, 0.0))
        return ((0.0, 0.0), (pitch, 0.0))
    side = int(ceil(sqrt(cuts)))
    offsets: list[tuple[float, float]] = []
    origin = (side - 1) * pitch * 0.5
    for row in range(side):
        for col in range(side):
            offsets.append((col * pitch - origin, row * pitch - origin))
            if len(offsets) >= cuts:
                return tuple(offsets)
    return tuple(offsets)


def _via_array_spacing_um(pdk: object, via: str, default_um: float) -> float:
    rules = getattr(pdk, "rules", None)
    for attr in ("array_spacing_um", "min_spacing_um"):
        try:
            return max(float(getattr(rules, attr)(str(via))), float(default_um))
        except Exception:
            continue
    return float(default_um)


def _min_width_um(pdk: object, layer: str, default_um: float) -> float:
    try:
        return max(float(getattr(pdk, "rules").min_width_um(str(layer))), float(default_um))
    except Exception:
        return float(default_um)


def _min_area_square_half_um(pdk: object, layer: str, default_um: float) -> float:
    min_area_um2 = _min_area_um2(pdk, layer, 0.0)
    if min_area_um2 <= 0.0:
        return float(default_um)
    return max((min_area_um2 ** 0.5) * 0.5, float(default_um))


def _min_area_um2(pdk: object, layer: str, default_um2: float) -> float:
    rules = getattr(pdk, "rules", None)
    try:
        min_area_nm2 = dict(getattr(rules, "min_area_nm2", {}) or {}).get(str(layer))
    except Exception:
        min_area_nm2 = None
    if min_area_nm2 is None:
        return float(default_um2)
    try:
        min_area_um2 = max(float(min_area_nm2), 0.0) * 1e-6
    except (TypeError, ValueError):
        return float(default_um2)
    return max(min_area_um2, float(default_um2))


def _enclosure_um(pdk: object, inner_layer: str, outer_layer: str, default_um: float) -> float:
    rules = getattr(pdk, "rules", None)
    for key in (f"{inner_layer}_{outer_layer}", f"{outer_layer}_{inner_layer}"):
        try:
            return max(float(rules.enclosure_um(key)), float(default_um))
        except Exception:
            try:
                raw_nm = dict(getattr(rules, "enclosure_nm", {}) or {}).get(key)
                if raw_nm is not None:
                    return max(float(raw_nm) * 1e-3, float(default_um))
            except Exception:
                continue
    return float(default_um)


def _snap_point(pdk: object, point: tuple[float, float]) -> tuple[float, float]:
    try:
        return tuple(getattr(pdk, "rules").snap_point_um(point))  # type: ignore[return-value]
    except Exception:
        return tuple(round(float(v), 6) for v in point)  # type: ignore[return-value]


def _snap_bbox(pdk: object, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    try:
        return tuple(getattr(pdk, "rules").snap_bbox_um(bbox, mode="outward"))  # type: ignore[return-value]
    except Exception:
        return tuple(round(float(v), 6) for v in bbox)  # type: ignore[return-value]


def _is_cut_layer(pdk: object, layer: str) -> bool:
    layer_name = str(layer)
    layer_map = getattr(pdk, "layer_map", None)
    contact = str(getattr(layer_map, "contact", "") or "")
    vias = {str(item) for item in tuple(getattr(layer_map, "vias", ()) or ())}
    return bool(layer_name and (layer_name == contact or layer_name in vias))


def _snap_exact_size_bbox_around_center(pdk: object, bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    rules = getattr(pdk, "rules", None)
    grid_nm = max(1, int(getattr(rules, "grid_nm", 1) or 1))
    x0, y0, x1, y1 = (float(value) for value in bbox)
    width_grid = max(1, int(round(abs(x1 - x0) * 1000.0 / grid_nm)))
    height_grid = max(1, int(round(abs(y1 - y0) * 1000.0 / grid_nm)))
    cx_grid = ((x0 + x1) * 0.5) * 1000.0 / grid_nm
    cy_grid = ((y0 + y1) * 0.5) * 1000.0 / grid_nm
    x0_grid = int(round(cx_grid - 0.5 * width_grid))
    y0_grid = int(round(cy_grid - 0.5 * height_grid))

    def to_um(grid_value: int) -> float:
        return round(grid_value * grid_nm * 1e-3, 12)

    return (
        to_um(x0_grid),
        to_um(y0_grid),
        to_um(x0_grid + width_grid),
        to_um(y0_grid + height_grid),
    )


def _route_span_bbox(
    points: Sequence[tuple[float, float]],
    boxes: Sequence[tuple[float, float, float, float]],
    *,
    margin_um: float = 0.25,
) -> tuple[float, float, float, float]:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    for x0, y0, x1, y1 in boxes:
        xs.extend((x0, x1))
        ys.extend((y0, y1))
    if not xs or not ys:
        return (0.0, 0.0, 0.5, 0.5)
    return (
        min(xs) - margin_um,
        min(ys) - margin_um,
        max(xs) + margin_um,
        max(ys) + margin_um,
    )


def _bbox_tracks_to_um(bbox: tuple[int, int, int, int], track_pitch_um: float) -> tuple[float, float, float, float]:
    pitch = max(float(track_pitch_um), 1e-6)
    return tuple(float(value) * pitch for value in bbox)  # type: ignore[return-value]


def _center_x(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[0] + bbox[2]) / 2.0


def _center_y(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[1] + bbox[3]) / 2.0


def _clamp(value: float, lower: float, upper: float) -> float:
    if lower > upper:
        lower, upper = upper, lower
    return max(lower, min(upper, value))


def _analog_block_rule_config(
    pdk: object | None,
    block: str,
    corridor_names: Sequence[str],
    default_critical: Mapping[str, int],
    default_noncritical: Mapping[str, int],
    *,
    default_target_aspect: tuple[int, int],
) -> dict[str, object]:
    site = smt_site_nm(pdk)
    block_rules = _block_smt_rules(pdk, block)
    rule_strategy = resolve_smt_rule_strategy(pdk, block)
    placement = _mapping(block_rules.get("hierarchical_placement", {}))
    routing = _mapping(block_rules.get("routing_resource", {}))
    pair_placement = _mapping(block_rules.get("pair_placement", {}))
    corridor_defaults = _mapping(routing.get("corridor_defaults", {}))
    corridor_overrides = _mapping(routing.get("corridors", {}))
    pair_spacing_nm_by_role = _mapping(pair_placement.get("spacing_nm_by_role", {}))
    pair_spacing_um_by_role = {
        str(role): max(float(value), 0.0) * 1e-3
        for role, value in pair_spacing_nm_by_role.items()
        if _is_number_like(value)
    }

    placement_spacing_tracks = nm_to_sites(placement.get("minimum_group_spacing_nm", 0), site_nm=site)
    target_aspect = _mapping(placement.get("target_aspect", {}))
    default_num, default_den = default_target_aspect
    target_aspect_num = _positive_int(target_aspect.get("num", default_num), default_num)
    target_aspect_den = _positive_int(target_aspect.get("den", default_den), default_den)

    default_pitch_sites = nm_to_sites(
        corridor_defaults.get("pitch_nm", routing.get("corridor_pitch_nm", site)),
        site_nm=site,
        minimum=1,
    )
    default_channel_gap_sites = nm_to_sites(
        corridor_defaults.get("channel_gap_nm", routing.get("default_channel_gap_nm", site)),
        site_nm=site,
        minimum=0,
    )
    default_fixed_reserved_tracks = _nonnegative_int(
        corridor_defaults.get("fixed_reserved_tracks", routing.get("default_fixed_reserved_tracks", 1)),
        1,
    )
    default_estimated_noncritical_tracks = _nonnegative_int(
        corridor_defaults.get("estimated_noncritical_tracks", routing.get("default_estimated_noncritical_tracks", 0)),
        0,
    )
    default_base_capacity_tracks = _nonnegative_int(
        corridor_defaults.get("base_capacity_tracks", routing.get("default_base_capacity_tracks", 0)),
        0,
    )
    default_capacity_consumes_gap = bool(corridor_defaults.get("capacity_consumes_gap", routing.get("capacity_consumes_gap", False)))
    default_require_orthogonal_overlap = bool(corridor_defaults.get("require_orthogonal_overlap", routing.get("require_orthogonal_overlap", True)))

    def corridor_rule(name: str) -> dict[str, object]:
        row = _mapping(corridor_overrides.get(name, {}))
        return {
            "base_capacity_tracks": _nonnegative_int(row.get("base_capacity_tracks", default_base_capacity_tracks), default_base_capacity_tracks),
            "estimated_noncritical_tracks": _nonnegative_int(row.get("estimated_noncritical_tracks", default_estimated_noncritical_tracks), default_estimated_noncritical_tracks),
            "fixed_reserved_tracks": _nonnegative_int(row.get("fixed_reserved_tracks", default_fixed_reserved_tracks), default_fixed_reserved_tracks),
            "pitch_sites": nm_to_sites(row.get("pitch_nm", default_pitch_sites * site), site_nm=site, minimum=1),
            "require_orthogonal_overlap": bool(row.get("require_orthogonal_overlap", default_require_orthogonal_overlap)),
            "capacity_consumes_gap": bool(row.get("capacity_consumes_gap", default_capacity_consumes_gap)),
            "channel_gap_sites": nm_to_sites(row.get("channel_gap_nm", default_channel_gap_sites * site), site_nm=site, minimum=0),
        }

    critical_track_demand = dict(default_critical)
    _overlay_int(critical_track_demand, _mapping(routing.get("critical_track_demand", {})))
    noncritical_track_demand = dict(default_noncritical)
    _overlay_int(noncritical_track_demand, _mapping(routing.get("noncritical_track_demand", {})))

    return {
        "site_nm": site,
        "placement_spacing_tracks": placement_spacing_tracks,
        "target_aspect_num": target_aspect_num,
        "target_aspect_den": target_aspect_den,
        "corridors": {name: corridor_rule(name) for name in corridor_names},
        "critical_track_demand": critical_track_demand,
        "noncritical_track_demand": noncritical_track_demand,
        "pair_spacing_um_by_role": pair_spacing_um_by_role,
        "configuration_path": f"metadata.smt_design_rules.{block}",
        "enabled": bool(_smt_design_rules_root(pdk).get("enabled", False)),
        "smt_mode": str(rule_strategy.get("mode", "hybrid")),
        "smt_solver_timeout_ms": _positive_int(rule_strategy.get("timeout_ms", 15_000), 15_000),
        "rule_strategy": dict(rule_strategy),
        "rule_family_owners": dict(_mapping(rule_strategy.get("rule_family_owners", {}))),
        "rule_owner_schema_version": rule_strategy.get("owner_schema_version"),
        "main_smt_hard_rule_families": tuple(rule_strategy.get("main_smt_hard_rule_families", ()) or ()),
        "main_smt_proxy_rule_families": tuple(rule_strategy.get("main_smt_proxy_rule_families", ()) or ()),
        "main_smt_rule_families": tuple(rule_strategy.get("main_smt_rule_families", ()) or ()),
        "local_smt_rule_families": tuple(rule_strategy.get("local_smt_rule_families", ()) or ()),
        "a_star_rule_families": tuple(rule_strategy.get("a_star_rule_families", ()) or ()),
        "eco_rule_families": tuple(rule_strategy.get("eco_rule_families", ()) or ()),
        "signoff_only_rule_families": tuple(rule_strategy.get("signoff_only_rule_families", ()) or ()),
        "external_eco_rule_families": tuple(rule_strategy.get("external_eco_rule_families", ()) or ()),
        "ignored_rule_families": tuple(rule_strategy.get("ignored_rule_families", ()) or ()),
    }


def _pair_spacing_um_by_role(rules: Mapping[str, object]) -> dict[str, float]:
    raw = _mapping(rules.get("pair_spacing_um_by_role", {}))
    result: dict[str, float] = {}
    for role, value in raw.items():
        try:
            result[str(role)] = max(float(value), 0.0)
        except (TypeError, ValueError):
            continue
    return result


def _is_number_like(value: object) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _bool_like(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no", "none"}
    return bool(value)


def _ldo_device_names(graph: TopologyGraph) -> dict[str, object]:
    devices = set(graph.devices)
    tail = "MTAIL" if "MTAIL" in devices else "M0"
    rtop = "RFB_TOP" if "RFB_TOP" in devices else "R1"
    rbot = "RFB_BOT" if "RFB_BOT" in devices else "R2"
    compensation_cap = "CCOMP" if "CCOMP" in devices else "COUT"
    compensation_devices = tuple(name for name in ("RCOMP", compensation_cap) if name in devices)
    missing = {"M1A", "M1B", "M3A", "M3B", tail, "MPASS", rtop, rbot, compensation_cap} - devices
    if missing:
        raise ValueError(f"LDO topology is missing devices required for SMT planning: {sorted(missing)}")
    return {
        "tail": tail,
        "input_pair": ("M1A", "M1B"),
        "load_pair": ("M3A", "M3B"),
        "pass": "MPASS",
        "feedback": (rtop, rbot),
        "output_cap": compensation_devices,
    }


def _ldo_group_specs(names: Mapping[str, object]) -> tuple[AnalogSmtGroupSpec, ...]:
    return (
        AnalogSmtGroupSpec("tail_source", (str(names["tail"]),), "row", 8, 6),
        AnalogSmtGroupSpec("input_pair", tuple(names["input_pair"]), "row", 12, 8),  # type: ignore[arg-type]
        AnalogSmtGroupSpec("load_pair", tuple(names["load_pair"]), "row", 12, 12),  # type: ignore[arg-type]
        AnalogSmtGroupSpec("pass_device", (str(names["pass"]),), "row", 14, 12),
        AnalogSmtGroupSpec("feedback_output", tuple(names["feedback"]), "row", 14, 10),  # type: ignore[arg-type]
        AnalogSmtGroupSpec("output_cap_bank", tuple(names["output_cap"]), "row", 8, 8),  # type: ignore[arg-type]
    )


def _bandgap_device_names(graph: TopologyGraph) -> dict[str, object]:
    devices = set(graph.devices)
    q2 = tuple(sorted((name for name in devices if name.startswith("Q2_")), key=_numeric_suffix)
    )
    r2 = tuple(sorted((name for name in devices if name.startswith("R2_")), key=_numeric_suffix)
    )
    missing = {"Q1", "R1", "M3A", "M3B", "M1A", "M1B", "M5A", "M5B", "M7"} - devices
    if missing or not q2 or not r2:
        raise ValueError(f"bandgap topology is missing devices required for SMT planning: {sorted(missing)}")
    return {
        "q1": "Q1",
        "bjt_core": ("Q1", *q2),
        "resistor_ladder": ("R1", *r2),
        "pmos_mirror": ("M3A", "M3B"),
        "input_pair": ("M1A", "M1B"),
        "load_pair": ("M5A", "M5B"),
        "tail": "M7",
    }


def _bandgap_group_specs(names: Mapping[str, object]) -> tuple[AnalogSmtGroupSpec, ...]:
    return (
        AnalogSmtGroupSpec("tail_source", (str(names["tail"]),), "row", 8, 6),
        AnalogSmtGroupSpec("input_pair", tuple(names["input_pair"]), "row", 12, 8),  # type: ignore[arg-type]
        AnalogSmtGroupSpec("load_pair", tuple(names["load_pair"]), "row", 12, 8),  # type: ignore[arg-type]
        AnalogSmtGroupSpec("bjt_core", tuple(names["bjt_core"]), "grid", 16, 10),  # type: ignore[arg-type]
        AnalogSmtGroupSpec("resistor_ladder", tuple(names["resistor_ladder"]), "row", 18, 8),  # type: ignore[arg-type]
        AnalogSmtGroupSpec("pmos_mirror", tuple(names["pmos_mirror"]), "row", 12, 8),  # type: ignore[arg-type]
    )


def _physical_group_from_spec(
    spec: AnalogSmtGroupSpec,
    device_sizes_um: DeviceSizeMap,
    track_pitch_um: float,
    *,
    spacing_um: float | None = None,
) -> HierarchicalPhysicalGroup2D:
    width, height = _group_size_tracks(spec, device_sizes_um, track_pitch_um, spacing_um=spacing_um)
    return HierarchicalPhysicalGroup2D(spec.name, width, height, allow_rotate=spec.allow_rotate)


def _group_size_tracks(
    spec: AnalogSmtGroupSpec,
    device_sizes_um: DeviceSizeMap,
    track_pitch_um: float,
    *,
    spacing_um: float | None = 0.5,
    margin_um: float = 0.5,
) -> tuple[int, int]:
    sizes = [_device_size_um(name, device_sizes_um) for name in spec.members]
    spacing = 0.5 if spacing_um is None else max(0.0, float(spacing_um))
    if spec.packing == "grid":
        cols = max(1, int(ceil(sqrt(len(sizes)))))
        rows = max(1, int(ceil(len(sizes) / cols)))
        max_w = max((w for w, _ in sizes), default=1.0)
        max_h = max((h for _, h in sizes), default=1.0)
        width_um = cols * max_w + max(0, cols - 1) * spacing + 2 * margin_um
        height_um = rows * max_h + max(0, rows - 1) * spacing + 2 * margin_um
    elif spec.packing == "column":
        width_um = max((w for w, _ in sizes), default=1.0) + 2 * margin_um
        height_um = sum(h for _, h in sizes) + max(0, len(sizes) - 1) * spacing + 2 * margin_um
    else:
        width_um = sum(w for w, _ in sizes) + max(0, len(sizes) - 1) * spacing + 2 * margin_um
        height_um = max((h for _, h in sizes), default=1.0) + 2 * margin_um
    pitch = max(float(track_pitch_um), 1e-6)
    return (
        max(spec.min_width_tracks, int(ceil(width_um / pitch))),
        max(spec.min_height_tracks, int(ceil(height_um / pitch))),
    )


def _place_row(
    group: HierarchicalGroupPlacement2D,
    members: Sequence[str],
    device_sizes_um: DeviceSizeMap,
    track_pitch_um: float,
    *,
    role: str,
    orient: str = "R0",
) -> tuple[Placement, ...]:
    gx, gy, gw, gh = _group_um(group, track_pitch_um)
    spacing = max(0.5, track_pitch_um)
    sizes = [_device_size_um(name, device_sizes_um) for name in members]
    total_w = sum(w for w, _ in sizes) + max(0, len(sizes) - 1) * spacing
    max_h = max((h for _, h in sizes), default=1.0)
    x = gx + max(0.0, (gw - total_w) / 2.0)
    y = gy + max(0.0, (gh - max_h) / 2.0)
    rows: list[Placement] = []
    for name, (width, _height) in zip(members, sizes):
        rows.append(Placement(str(name), x, y, orient=orient, role=role))
        x += width + spacing
    return tuple(rows)


def _place_symmetric_pair(
    group: HierarchicalGroupPlacement2D,
    pair: Sequence[str],
    device_sizes_um: DeviceSizeMap,
    track_pitch_um: float,
    *,
    role: str,
    spacing_um: float | None = None,
) -> tuple[Placement, ...]:
    if len(pair) != 2:
        return _place_row(group, pair, device_sizes_um, track_pitch_um, role=role)
    left, right = str(pair[0]), str(pair[1])
    gx, gy, gw, gh = _group_um(group, track_pitch_um)
    spacing = max(0.5, track_pitch_um) if spacing_um is None else max(float(spacing_um), track_pitch_um)
    lw, lh = _device_size_um(left, device_sizes_um)
    rw, rh = _device_size_um(right, device_sizes_um)
    total_w = lw + rw + spacing
    x = gx + max(0.0, (gw - total_w) / 2.0)
    y = gy + max(0.0, (gh - max(lh, rh)) / 2.0)
    return (
        Placement(left, x, y, orient="R0", role=role),
        Placement(right, x + lw + spacing, y, orient="MY", role=role),
    )


def _place_grid(
    group: HierarchicalGroupPlacement2D,
    members: Sequence[str],
    device_sizes_um: DeviceSizeMap,
    track_pitch_um: float,
    *,
    role: str,
    prefer_center: str = "",
) -> tuple[Placement, ...]:
    members = tuple(str(name) for name in members)
    if prefer_center and prefer_center in members and len(members) >= 3:
        others = tuple(name for name in members if name != prefer_center)
        middle = len(others) // 2
        members = others[:middle] + (prefer_center,) + others[middle:]
    gx, gy, gw, gh = _group_um(group, track_pitch_um)
    spacing = max(0.5, track_pitch_um)
    cols = max(1, int(ceil(sqrt(len(members)))))
    rows = max(1, int(ceil(len(members) / cols)))
    max_w = max((_device_size_um(name, device_sizes_um)[0] for name in members), default=1.0)
    max_h = max((_device_size_um(name, device_sizes_um)[1] for name in members), default=1.0)
    used_w = cols * max_w + max(0, cols - 1) * spacing
    used_h = rows * max_h + max(0, rows - 1) * spacing
    x0 = gx + max(0.0, (gw - used_w) / 2.0)
    y0 = gy + max(0.0, (gh - used_h) / 2.0)
    placements: list[Placement] = []
    for idx, name in enumerate(members):
        row = idx // cols
        col = idx % cols
        placements.append(Placement(name, x0 + col * (max_w + spacing), y0 + row * (max_h + spacing), orient="R0", role=role))
    return tuple(placements)


def _analog_smt_checks(
    block: str,
    graph: TopologyGraph,
    problem: HierarchicalPhysicalProblem2D,
    physical: HierarchicalPhysicalSolution2D,
    group_specs: Mapping[str, AnalogSmtGroupSpec],
) -> dict[str, object]:
    issues: list[str] = []
    if not physical.converged or not physical.routing.passed:
        issues.append("hierarchical SMT did not close route capacity")
    grouped_devices = {dev for spec in group_specs.values() for dev in spec.members}
    missing = sorted(set(graph.devices) - grouped_devices)
    if missing:
        issues.append(f"devices not represented in SMT groups: {missing}")
    missing_groups = sorted(set(group_specs) - set(physical.master.placements))
    if missing_groups:
        issues.append(f"groups missing from SMT solution: {missing_groups}")
    required_corridors = {corridor.name for corridor in problem.corridors}
    solved_corridors = set(physical.master.corridor_capacity_tracks)
    if required_corridors - solved_corridors:
        issues.append(f"corridors missing capacity assignments: {sorted(required_corridors - solved_corridors)}")
    return {
        "passed": not issues,
        "issues": tuple(issues),
        "block": block,
        "device_count": len(graph.devices),
        "group_count": len(group_specs),
        "critical_route_count": len(problem.critical_routes),
        "noncritical_route_count": len(problem.noncritical_routes),
        "refinement_iterations": len(physical.iterations),
        "total_width_tracks": physical.master.total_width_tracks,
        "total_height_tracks": physical.master.total_height_tracks,
        "corridor_capacity_tracks": dict(physical.master.corridor_capacity_tracks),
        "critical_load_by_corridor": dict(physical.master.critical_load_by_corridor),
        "selected_critical_candidates": dict(physical.master.critical_candidate_by_route),
    }


def _device_size_um(name: str, device_sizes_um: DeviceSizeMap) -> tuple[float, float]:
    try:
        width, height = device_sizes_um[name]
        return max(float(width), 0.1), max(float(height), 0.1)
    except (KeyError, TypeError, ValueError):
        return 1.0, 1.0


def _group_um(group: HierarchicalGroupPlacement2D, track_pitch_um: float) -> tuple[float, float, float, float]:
    pitch = max(float(track_pitch_um), 1e-6)
    return (
        group.x_tracks * pitch,
        group.y_tracks * pitch,
        group.width_tracks * pitch,
        group.height_tracks * pitch,
    )


def _crn28_mos_finger_rule_config(pdk: object) -> Mapping[str, object]:
    metadata = _metadata(pdk)
    direct = _mapping(metadata.get("mos_finger_constraints", {}))
    if direct:
        return direct
    sweep = _mapping(_mapping(metadata.get("pcell_drc_sweep", {})).get("strongarm_mos", {}))
    return _mapping(sweep.get("mos_finger_constraints", {}))


def _crn28_mos_pcell_overrides(pdk: object, variant_name: str) -> dict[str, dict[str, object]]:
    metadata = _metadata(pdk)
    sweep = _mapping(_mapping(metadata.get("pcell_drc_sweep", {})).get("strongarm_mos", {}))
    variant_params: dict[str, object] = {}
    for row in tuple(sweep.get("variants", ()) or ()):
        item = _mapping(row)
        if str(item.get("name", "")) == variant_name:
            variant_params = dict(_mapping(item.get("params", {})))
            break
    pmos_params = dict(_mapping(sweep.get("pmos_params", {})))
    overrides = {
        "nmos": dict(variant_params),
        "pmos": {**variant_params, **pmos_params},
    }
    direct = _mapping(metadata.get("mos_pcell_overrides", {}))
    for logical in ("nmos", "pmos"):
        if logical in direct:
            overrides[logical] = {**overrides[logical], **dict(_mapping(direct.get(logical, {})))}
    return overrides


def _mos_logical_name_for_device(device: object) -> str:
    model = str(getattr(device, "model", "") or "").lower()
    if "pmos" in model or model.startswith("pch") or model.startswith("p_"):
        return "pmos"
    if "nmos" in model or model.startswith("nch") or model.startswith("n_"):
        return "nmos"
    return ""


def _sizing_dimension_m(sizing: Mapping[str, object], keys: tuple[str, ...], default_m: float) -> float:
    for key in keys:
        if key in sizing:
            try:
                return float(sizing[key])
            except (TypeError, ValueError):
                return default_m
        nm_key = f"{key}_nm"
        if nm_key in sizing:
            try:
                return float(sizing[nm_key]) * 1e-9
            except (TypeError, ValueError):
                return default_m
        um_key = f"{key}_um"
        if um_key in sizing:
            try:
                return float(sizing[um_key]) * 1e-6
            except (TypeError, ValueError):
                return default_m
    return default_m


def _metadata(pdk: object | None) -> Mapping[str, object]:
    metadata = getattr(pdk, "metadata", {}) if pdk is not None else {}
    return metadata if isinstance(metadata, Mapping) else {}


def _smt_design_rules_root(pdk: object | None) -> Mapping[str, object]:
    root = _metadata(pdk).get("smt_design_rules", {})
    if not isinstance(root, Mapping):
        return {}
    if root and not bool(root.get("enabled", True)):
        return {}
    return root


def _block_smt_rules(pdk: object | None, block: str) -> Mapping[str, object]:
    row = _smt_design_rules_root(pdk).get(block, {})
    return row if isinstance(row, Mapping) else {}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _net_via_bool(config: Mapping[str, object], net: str, via: str) -> bool:
    value = _net_via_lookup(config, net, via)
    if value is None:
        return False
    if isinstance(value, (tuple, list, set, frozenset)):
        via_keys = {str(via), str(via).lower(), str(via).upper()}
        return any(str(item) in via_keys for item in value)
    return _bool_like(value)


def _net_via_positive_int(config: Mapping[str, object], net: str, via: str, default: int) -> int:
    value = _net_via_lookup(config, net, via)
    if value is None or isinstance(value, (tuple, list, set, frozenset)):
        return _positive_int(default, default)
    return _positive_int(value, default)


def _net_via_lookup(config: Mapping[str, object], net: str, via: str) -> object | None:
    net_keys = (str(net), str(net).lower(), str(net).upper(), "*", "default")
    row: object | None = None
    for key in net_keys:
        if key in config:
            row = config[key]
            break
    if row is None:
        return None
    if isinstance(row, Mapping):
        via_keys = (str(via), str(via).lower(), str(via).upper(), "*", "default")
        for key in via_keys:
            if key in row:
                return row[key]
        return None
    return row


def _overlay_int(target: dict[str, int], source: Mapping[str, object]) -> None:
    for key, value in source.items():
        target[str(key)] = _nonnegative_int(value, target.get(str(key), 0))


def _nonnegative_int(value: object, default: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return max(int(default), 0)


def _positive_int(value: object, default: int) -> int:
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return max(int(default), 1)


def _positive_float(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if result <= 0.0:
        result = float(default)
    return max(result, 1e-12)


def _nonnegative_float(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if result < 0.0:
        result = float(default)
    return max(result, 0.0)


def _factor_pairs(value: int) -> tuple[tuple[int, int], ...]:
    count = max(1, int(value))
    pairs: list[tuple[int, int]] = []
    limit = int(sqrt(count)) + 1
    for rows in range(1, limit + 1):
        if count % rows:
            continue
        cols = count // rows
        pairs.append((rows, cols))
        if rows != cols:
            pairs.append((cols, rows))
    return tuple(sorted(set(pairs), key=lambda item: (abs(item[0] - item[1]), item[0], item[1])))


def _numeric_suffix(name: str) -> int:
    try:
        return int(name.rsplit("_", 1)[1])
    except (IndexError, ValueError):
        return 0
