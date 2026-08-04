"""Macro refinement candidates for compact analog SMT placement.

The flat analog SMT compiler places pattern boxes.  For human-quality packing,
some boxes are too coarse: a resistor ladder, capacitor bank, or passive array
may need to be split into legal sub-patterns before the solver can use local
empty space.  This module keeps that refinement generic and explicit.  It does
not encode a final placement; it only creates alternative DSL specs plus a
mapping from original macro names to the generated sub-pattern names.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import ceil
from typing import Mapping, Sequence

from analogskills.contracts import TopologyGraph
from analogskills.layout.analog_layout_dsl import (
    AnalogLayoutSpec,
    DevicePatternSpec,
    LayoutObjectiveTermSpec,
    PCellRealizationCandidateSpec,
    PCellRealizationGroupSpec,
    PackConstraintSpec,
    PatternRelationSpec,
)


@dataclass(frozen=True)
class MacroRefinementCandidateSpec:
    """One legal macro-refinement alternative for an analog layout spec."""

    name: str
    spec: AnalogLayoutSpec
    macro_subpatterns: Mapping[str, tuple[str, ...]]
    notes: str = ""
    metadata: Mapping[str, object] | None = None


def baseline_macro_refinement(spec: AnalogLayoutSpec) -> MacroRefinementCandidateSpec:
    """Return the unmodified spec as a refinement candidate."""

    # Keep the baseline candidate isolated from refinement generators.  The DSL
    # dataclasses are frozen, but explicit tuple copies make this safe if future
    # generators reuse or normalize candidate containers before solving.
    baseline_spec = replace(
        spec,
        patterns=tuple(spec.patterns),
        pairs=tuple(spec.pairs),
        relations=tuple(spec.relations),
        critical_nets=tuple(spec.critical_nets),
        route_resources=tuple(spec.route_resources),
        pack_constraints=tuple(spec.pack_constraints),
        pcell_realization_groups=tuple(spec.pcell_realization_groups),
    )
    return MacroRefinementCandidateSpec(
        "baseline_macro",
        baseline_spec,
        {pattern.name: (pattern.name,) for pattern in baseline_spec.patterns},
        notes="Unmodified macro pattern layout spec.",
        metadata={"kind": "baseline"},
    )


def current_passive_realization_guard(spec: AnalogLayoutSpec) -> MacroRefinementCandidateSpec | None:
    """Return a signoff-safe candidate that disables exploratory passive aspects.

    Passive aspect candidates are useful for layout exploration, but native
    passive PCells often need aspect-specific terminal/access calibration before
    they can be trusted for signoff.  This candidate keeps MOS/BJT choices intact
    and filters passive realization groups down to their ``*_current`` candidate.
    The outer refinement selector can then compare it against exploratory
    candidates using true bbox area.
    """

    guarded_groups: list[PCellRealizationGroupSpec] = []
    changed = False
    for group in tuple(spec.pcell_realization_groups):
        candidates = tuple(group.candidates)
        if len(candidates) <= 1 or not _looks_like_passive_realization_group(group):
            guarded_groups.append(group)
            continue
        current = tuple(candidate for candidate in candidates if _is_current_passive_candidate(candidate))
        if not current:
            guarded_groups.append(group)
            continue
        guarded_groups.append(replace(group, candidates=current))
        changed = True
    if not changed:
        return None

    guarded_spec = replace(
        spec,
        pcell_realization_groups=tuple(guarded_groups),
        notes=(spec.notes + "\nMacro refinement: passive aspect guard keeps current calibrated passive realization.").strip(),
    )
    return MacroRefinementCandidateSpec(
        "current_passive_guard",
        guarded_spec,
        {pattern.name: (pattern.name,) for pattern in guarded_spec.patterns},
        notes="Guard candidate using current passive PCell realization only; MOS realization choices remain enabled.",
        metadata={"kind": "passive_realization_guard", "policy": "current_only"},
    )


def ldo_human_motif_refinement_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Generate alternative LDO floorplan motifs without fixing coordinates.

    The reference LDO is not copied geometrically.  Only its durable hierarchy
    is retained: a regular power island on one side, a matched error-amplifier
    core, a feedback interface near the pass gate/output, and an output
    capacitor bank that remains part of the block.  Each candidate fixes at
    most the coarse topology; exact coordinates, PCell realizations, gaps, and
    local packing remain SMT variables.
    """

    del graph  # The candidate is structural; pattern presence is sufficient.
    pattern_names = {pattern.name for pattern in spec.patterns}
    required = {
        "tail_source",
        "input_pair",
        "load_pair",
        "pass_device",
        "feedback_output",
        "output_cap_bank",
    }
    if not required <= pattern_names:
        return ()

    candidates: list[MacroRefinementCandidateSpec] = []

    # A weak reference prior: the pass macro is a right-side island, while the
    # solver remains free to arrange the complete control/passive region.
    weak_choices = {
        ("load_pair", "pass_device"): "right_of",
        ("feedback_output", "pass_device"): "right_of",
        ("output_cap_bank", "pass_device"): "right_of",
    }
    weak_spec, weak_fixed = _rewrite_ldo_motif_relations(
        spec,
        weak_choices,
        hard=True,
        keep_unselected_soft=True,
    )
    if len(weak_fixed) == len(weak_choices):
        candidates.append(
            MacroRefinementCandidateSpec(
                "ldo_weak_right_power_island",
                weak_spec,
                {pattern.name: (pattern.name,) for pattern in weak_spec.patterns},
                notes=(
                    "Keep only the reference-level right-side pass island as a "
                    "coarse topology; leave the control and passive packing free."
                ),
                metadata={
                    "kind": "ldo_human_motif",
                    "motif_strength": "weak",
                    "fixed_relations": weak_fixed,
                    "pcell_realization_policy": "keep_all_smt_candidates",
                },
            )
        )

    # A stronger but still coordinate-free partition.  It forms a conventional
    # tail/input/load core and keeps the output capacitor below that core.
    partition_choices = {
        **weak_choices,
        ("tail_source", "input_pair"): "above",
        ("input_pair", "load_pair"): "above",
        ("output_cap_bank", "input_pair"): "above",
    }
    partition_spec, partition_fixed = _rewrite_ldo_motif_relations(
        spec,
        partition_choices,
        hard=True,
        keep_unselected_soft=True,
    )
    if len(partition_fixed) == len(partition_choices):
        candidates.append(
            MacroRefinementCandidateSpec(
                "ldo_reference_partition",
                partition_spec,
                {pattern.name: (pattern.name,) for pattern in partition_spec.patterns},
                notes=(
                    "Reference-inspired partition: power island right, matched "
                    "amplifier core, capacitor below the control region."
                ),
                metadata={
                    "kind": "ldo_human_motif",
                    "motif_strength": "partition",
                    "fixed_relations": partition_fixed,
                    "pcell_realization_policy": "keep_all_smt_candidates",
                },
            )
        )

    # A deliberately unconstrained global-packing alternative.  Pair matching,
    # no-overlap, PCell legality, pack windows, route HPWL and block aspect stay
    # active, but accumulated directional preferences cannot silently become a
    # rigid topology.
    free_relations = tuple(relation for relation in spec.relations if relation.hard and not relation.candidates)
    free_terms = tuple(
        term
        for term in spec.objective_terms
        if term.name
        not in {
            "cap_core_left_edge_alignment",
            "amp_row_alignment",
            "ldo_power_feedback_regular_spacing",
            "ldo_left_macro_vertical_coordination",
        }
    )
    free_spec = replace(
        spec,
        relations=free_relations,
        objective_terms=free_terms,
        notes=(
            spec.notes
            + "\nMacro refinement: global free packing keeps electrical/matching "
            "constraints and compactness objectives but removes directional priors."
        ).strip(),
    )
    candidates.append(
        MacroRefinementCandidateSpec(
            "ldo_global_free_packing",
            free_spec,
            {pattern.name: (pattern.name,) for pattern in free_spec.patterns},
            notes=(
                "Global packing candidate with no soft direction field; SMT "
                "chooses the topology from area, aspect, HPWL and local motifs."
            ),
            metadata={
                "kind": "ldo_global_free_packing",
                "motif_strength": "none",
                "removed_soft_relation_count": len(spec.relations) - len(free_relations),
                "pcell_realization_policy": "keep_all_smt_candidates",
            },
        )
    )
    return tuple(candidates)


def _rewrite_ldo_motif_relations(
    spec: AnalogLayoutSpec,
    selected: Mapping[tuple[str, str], str],
    *,
    hard: bool,
    keep_unselected_soft: bool,
) -> tuple[AnalogLayoutSpec, dict[str, str]]:
    rewritten: list[PatternRelationSpec] = []
    fixed: dict[str, str] = {}
    for relation in spec.relations:
        key = (relation.source, relation.target)
        selected_kind = selected.get(key)
        candidates = tuple(str(item).lower() for item in relation.candidates)
        if selected_kind is None:
            if keep_unselected_soft:
                rewritten.append(relation)
            continue
        if selected_kind not in candidates and relation.kind != selected_kind:
            rewritten.append(relation)
            continue
        rewritten.append(
            replace(
                relation,
                kind=selected_kind,
                candidates=(),
                candidate_costs={},
                hard=bool(hard),
                notes=(
                    relation.notes
                    + f" LDO motif candidate selects {relation.source}->{relation.target}={selected_kind}."
                ).strip(),
            )
        )
        fixed[f"{relation.source}->{relation.target}"] = selected_kind
    return (
        replace(
            spec,
            relations=tuple(rewritten),
            notes=(spec.notes + "\nLDO human-motif alternative generated for multi-candidate SMT.").strip(),
        ),
        fixed,
    )


def bandgap_resistor_ladder_refinement_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
    *,
    max_candidates: int = 4,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Generate resistor-ladder split candidates for Brokaw-like bandgaps.

    The detection is structural: a macro named ``resistor_ladder`` must contain
    ``R1`` and at least one ``R2_*`` device.  The returned candidates keep the
    series order but replace the coarse ladder box with row sub-patterns.
    """

    pattern = _pattern_by_name(spec).get("resistor_ladder")
    if pattern is None:
        return ()
    resistors = tuple(pattern.devices)
    if "R1" not in resistors or not any(name.startswith("R2_") for name in resistors):
        return ()
    ordered = tuple(sorted(resistors, key=_bandgap_resistor_order_key))
    candidates: list[MacroRefinementCandidateSpec] = []
    for rows in (3, 2):
        if len(candidates) >= max(0, int(max_candidates)):
            break
        if rows <= 1 or len(ordered) <= rows:
            continue
        candidate = _split_resistor_ladder_into_rows(spec, ordered, rows=rows)
        if candidate is not None:
            candidates.append(candidate)
    return tuple(candidates)


def bandgap_free_global_packing_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Generate a coordinate-free, objective-driven bandgap packing option.

    Matching pairs, device non-overlap, PCell legality, and routing rules stay
    intact.  Coarse above/left/right relations are deliberately removed from
    this candidate: they describe a conventional sketch, not an electrical
    requirement.  The solver is instead given soft compact-envelope and row
    alignment objectives, so a one-row MOS strip, a two-row analog core, or a
    mixed packing can compete on true area and critical-net length.
    """

    del graph
    pattern_names = {pattern.name for pattern in spec.patterns}
    mos_patterns = ("pmos_mirror", "load_pair", "input_pair", "tail_source")
    if not set(mos_patterns) <= pattern_names or "bjt_core" not in pattern_names:
        return ()

    directional = {"above", "below", "left_of", "right_of"}
    relations = tuple(
        relation
        for relation in spec.relations
        if str(relation.kind).lower() not in directional
        and not directional.intersection(str(item).lower() for item in tuple(relation.candidates))
    )
    packs = tuple(spec.pack_constraints) + (
        PackConstraintSpec(
            "free_global_mos_pack",
            mos_patterns,
            max_width_um=None,
            max_height_um=None,
            weight=44,
            width_weight=14,
            height_weight=14,
            area_weight=8,
            notes="Direction-free MOS packing window; topology is selected by the global objective.",
        ),
    )
    objective_terms = tuple(spec.objective_terms) + (
        LayoutObjectiveTermSpec(
            "free_global_mos_row_alignment",
            "edge_alignment",
            patterns=mos_patterns,
            weight=18,
            axis="y",
            notes="Softly reward one coordinated MOS band without making it a placement constraint.",
        ),
        LayoutObjectiveTermSpec(
            "free_global_mos_compact_envelope",
            "compact_envelope",
            patterns=mos_patterns,
            weight=24,
            axis="both",
            notes="Let true PCell envelopes decide between one-row and multi-row MOS packing.",
        ),
    )
    objective = replace(
        spec.objective,
        bbox_weight=max(int(spec.objective.bbox_weight), 240),
        width_weight=max(int(spec.objective.width_weight), 34),
        height_weight=max(int(spec.objective.height_weight), 16),
        area_weight=max(int(spec.objective.area_weight), 55),
        max_side_weight=max(int(spec.objective.max_side_weight), 90),
        right_whitespace_weight=max(int(spec.objective.right_whitespace_weight), 24),
    )
    refined = replace(
        spec,
        relations=relations,
        pack_constraints=packs,
        objective_terms=objective_terms,
        objective=objective,
        notes=(
            spec.notes
            + "\nMacro refinement: direction-free global packing; only matching, legality, non-overlap, and routing remain hard."
        ).strip(),
    )
    return (
        MacroRefinementCandidateSpec(
            "free_global_packing",
            refined,
            {pattern.name: (pattern.name,) for pattern in refined.patterns},
            notes="No fixed macro directions; compactness and critical routing choose the topology.",
            metadata={
                "kind": "objective_driven_global_packing",
                "fixed_relations": {},
                "removed_directional_relation_count": len(spec.relations) - len(relations),
                "pcell_realization_policy": "keep_all_smt_candidates",
            },
        ),
    )


def bandgap_upper_mos_compaction_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Generate local MOS-side compaction candidates for Brokaw-like bandgaps.

    This does not prescribe a full layout.  It only fixes one local relation
    that current observations show can remove a one-track top slack region:
    place the PMOS mirror to the left of the input pair while preserving the
    existing PMOS/load same-row/right-of constraints.
    """

    pattern_names = {pattern.name for pattern in spec.patterns}
    if not {"input_pair", "pmos_mirror", "load_pair", "resistor_ladder", "bjt_core"} <= pattern_names:
        return ()
    compact_relation_choices = {
        ("tail_source", "input_pair"): "right_of",
        ("input_pair", "load_pair"): "above",
        ("input_pair", "pmos_mirror"): "left_of",
        ("bjt_core", "input_pair"): "above",
        ("bjt_core", "tail_source"): "above",
    }
    rewritten: list[PatternRelationSpec] = []
    fixed_relations: dict[str, str] = {}
    for relation in spec.relations:
        key = (relation.source, relation.target)
        selected_kind = compact_relation_choices.get(key)
        candidates = tuple(str(item).lower() for item in relation.candidates)
        if selected_kind is not None and selected_kind in candidates:
            rewritten.append(
                replace(
                    relation,
                    kind=selected_kind,
                    candidates=(),
                    candidate_costs={},
                    notes=(relation.notes + f" Macro refinement: fix {relation.source}->{relation.target}={selected_kind} for compact upper MOS packing.").strip(),
                )
            )
            fixed_relations[f"{relation.source}->{relation.target}"] = selected_kind
        else:
            rewritten.append(relation)
    if fixed_relations.get("input_pair->pmos_mirror") != "left_of":
        return ()
    refined = replace(
        spec,
        relations=tuple(rewritten),
        pcell_realization_groups=(),
        notes=(spec.notes + "\nMacro refinement: PMOS mirror side-attached left of input pair.").strip(),
    )
    return (
        MacroRefinementCandidateSpec(
            "upper_mos_pmos_left_attach",
            refined,
            {pattern.name: (pattern.name,) for pattern in refined.patterns},
            notes="Fix local input_pair->pmos_mirror relation to left_of to reduce upper MOS stack slack.",
            metadata={
                "kind": "local_relation_refinement",
                "fixed_relations": fixed_relations,
                "pcell_realization_policy": "use_current_device_sizes",
            },
        ),
    )


def bandgap_vertical_gap_compaction_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Generate a utilization-first vertical channel compaction candidate.

    The candidate preserves the proven baseline macro topology and only reduces
    two hard vertical channels that directly determine the global top edge:
    BJT→resistor and input→load.  It uses the current device sizes instead of
    opening PCell realization choices, so the experiment measures placement
    utilization rather than nf/aspect exploration.
    """

    pattern_names = {pattern.name for pattern in spec.patterns}
    if not {"input_pair", "pmos_mirror", "load_pair", "resistor_ladder", "bjt_core", "tail_source"} <= pattern_names:
        return ()
    compact_relation_choices = {
        ("tail_source", "input_pair"): "right_of",
        ("input_pair", "load_pair"): "above",
        ("input_pair", "pmos_mirror"): "above",
        ("bjt_core", "input_pair"): "above",
        ("bjt_core", "tail_source"): "above",
    }
    gap_overrides = {
        ("bjt_core", "resistor_ladder"): 0.5,
        ("input_pair", "load_pair"): 1.0,
    }
    rewritten: list[PatternRelationSpec] = []
    fixed_relations: dict[str, str] = {}
    applied_gaps: dict[str, float] = {}
    for relation in spec.relations:
        key = (relation.source, relation.target)
        selected_kind = compact_relation_choices.get(key)
        candidates = tuple(str(item).lower() for item in relation.candidates)
        updated = relation
        if selected_kind is not None and selected_kind in candidates:
            updated = replace(
                updated,
                kind=selected_kind,
                candidates=(),
                candidate_costs={},
                notes=(updated.notes + f" Macro refinement: fix {relation.source}->{relation.target}={selected_kind} for utilization compaction.").strip(),
            )
            fixed_relations[f"{relation.source}->{relation.target}"] = selected_kind
        if key in gap_overrides:
            updated = replace(
                updated,
                min_gap_um=float(gap_overrides[key]),
                notes=(updated.notes + f" Macro refinement: reduce min_gap to {float(gap_overrides[key]):g}um for utilization compaction.").strip(),
            )
            applied_gaps[f"{relation.source}->{relation.target}"] = float(gap_overrides[key])
        rewritten.append(updated)
    if fixed_relations.get("tail_source->input_pair") != "right_of" or not applied_gaps:
        return ()
    refined = replace(
        spec,
        relations=tuple(rewritten),
        pcell_realization_groups=(),
        notes=(spec.notes + "\nMacro refinement: compact vertical channels for improved device utilization.").strip(),
    )
    return (
        MacroRefinementCandidateSpec(
            "vertical_gap_compaction",
            refined,
            {pattern.name: (pattern.name,) for pattern in refined.patterns},
            notes="Reduce BJT/resistor and input/load vertical gaps while preserving baseline topology.",
            metadata={
                "kind": "gap_refinement",
                "fixed_relations": fixed_relations,
                "min_gap_um_overrides": applied_gaps,
                "pcell_realization_policy": "use_current_device_sizes",
            },
        ),
    )


def bandgap_top_mos_void_insertion_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Generate a candidate that inserts the tail device into top MOS whitespace.

    Current bandgap observations show a legal slot between the input pair and
    the load/PMOS row.  This candidate preserves the baseline BJT/resistor
    topology, moves ``tail_source`` to the right side of ``input_pair``, and
    reduces the local upper MOS channel gaps enough for the tail row to occupy
    that slot.
    """

    pattern_names = {pattern.name for pattern in spec.patterns}
    if not {"input_pair", "pmos_mirror", "load_pair", "resistor_ladder", "bjt_core", "tail_source"} <= pattern_names:
        return ()
    compact_relation_choices = {
        ("tail_source", "input_pair"): "left_of",  # DSL semantics: source right of target.
        ("input_pair", "load_pair"): "above",
        ("input_pair", "pmos_mirror"): "above",
        ("bjt_core", "resistor_ladder"): "right_of",
        ("bjt_core", "input_pair"): "above",
        ("bjt_core", "tail_source"): "above",
    }
    gap_overrides = {
        ("bjt_core", "resistor_ladder"): 0.5,
        ("input_pair", "load_pair"): 0.0,
        ("input_pair", "pmos_mirror"): 0.5,
    }
    rewritten: list[PatternRelationSpec] = []
    fixed_relations: dict[str, str] = {}
    applied_gaps: dict[str, float] = {}
    for relation in spec.relations:
        key = (relation.source, relation.target)
        selected_kind = compact_relation_choices.get(key)
        candidates = tuple(str(item).lower() for item in relation.candidates)
        updated = relation
        if selected_kind is not None and selected_kind in candidates:
            updated = replace(
                updated,
                kind=selected_kind,
                hard=True,
                candidates=(),
                candidate_costs={},
                notes=(updated.notes + f" Macro refinement: fix {relation.source}->{relation.target}={selected_kind} for top MOS void insertion.").strip(),
            )
            fixed_relations[f"{relation.source}->{relation.target}"] = selected_kind
        if key in gap_overrides:
            updated = replace(
                updated,
                min_gap_um=float(gap_overrides[key]),
                notes=(updated.notes + f" Macro refinement: reduce min_gap to {float(gap_overrides[key]):g}um for top MOS void insertion.").strip(),
            )
            applied_gaps[f"{relation.source}->{relation.target}"] = float(gap_overrides[key])
        rewritten.append(updated)
    if fixed_relations.get("tail_source->input_pair") != "left_of" or not applied_gaps:
        return ()
    patterns: list[DevicePatternSpec] = []
    fixed_patterns: dict[str, str] = {}
    for pattern in spec.patterns:
        if pattern.name == "resistor_ladder":
            snake = tuple(candidate for candidate in pattern.candidates if str(candidate.name).startswith("r_ladder_snake_"))
            if snake:
                patterns.append(replace(pattern, candidates=snake))
                fixed_patterns[pattern.name] = snake[0].name
                continue
        patterns.append(pattern)
    pack_constraints = _bandgap_top_mos_void_insertion_pack_windows(spec.pack_constraints)
    refined = replace(
        spec,
        patterns=tuple(patterns),
        relations=tuple(rewritten),
        pack_constraints=pack_constraints,
        pcell_realization_groups=_placement_relevant_realization_groups(spec.pcell_realization_groups),
        notes=(spec.notes + "\nMacro refinement: insert tail_source into top MOS whitespace.").strip(),
    )
    return (
        MacroRefinementCandidateSpec(
            "top_mos_tail_void_insertion",
            refined,
            {pattern.name: (pattern.name,) for pattern in refined.patterns},
            notes="Move tail_source into the observed top MOS void and compact upper MOS gaps.",
            metadata={
                "kind": "void_insertion_refinement",
                "inserted_macro": "tail_source",
                "void_region": "top_mos_between_input_load",
                "fixed_relations": fixed_relations,
                "fixed_patterns": fixed_patterns,
                "min_gap_um_overrides": applied_gaps,
                "pcell_realization_policy": "preserve_mos_and_calibrated_or_drawn_passive_compact_realizations",
            },
        ),
    )


def bandgap_reference_min_gap_packing_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Generate a reference-IP-style local packing candidate for bandgap.

    ADC/PLL reference layouts show a repeated pattern: keep device arrays as
    finite primitive motifs, then pack adjacent motifs near local rule minima
    with simple row/column alignment.  This candidate intentionally does not
    add a rich DSL.  It only hardens the local relations that define the right
    device column and adds center-alignment constraints so bounded SMT cannot
    ignore the packing intent as a soft post-score term.
    """

    pattern_names = {pattern.name for pattern in spec.patterns}
    required = {"input_pair", "pmos_mirror", "load_pair", "resistor_ladder", "bjt_core", "tail_source"}
    if not required <= pattern_names:
        return ()

    rewritten: list[PatternRelationSpec] = []
    fixed_relations: dict[str, str] = {}
    gap_overrides = {
        ("tail_source", "input_pair"): 0.0,
        ("input_pair", "load_pair"): 0.0,
        ("input_pair", "pmos_mirror"): 0.0,
        ("bjt_core", "resistor_ladder"): 0.5,
    }
    compact_relation_choices = {
        ("tail_source", "input_pair"): "above",
        ("input_pair", "load_pair"): "above",
        ("input_pair", "pmos_mirror"): "below",
        ("bjt_core", "resistor_ladder"): "right_of",
        ("bjt_core", "input_pair"): "right_of",
        ("bjt_core", "tail_source"): "right_of",
    }
    for relation in spec.relations:
        key = (relation.source, relation.target)
        selected_kind = compact_relation_choices.get(key)
        updated = relation
        if selected_kind is not None:
            updated = replace(
                updated,
                kind=selected_kind,
                min_gap_um=float(gap_overrides.get(key, relation.min_gap_um)),
                hard=True,
                candidates=(),
                candidate_costs={},
                notes=(
                    updated.notes
                    + f" Reference packing: harden {relation.source}->{relation.target}={selected_kind} so bounded SMT honors local min-gap motif adjacency."
                ).strip(),
            )
            fixed_relations[f"{relation.source}->{relation.target}"] = selected_kind
        rewritten.append(updated)

    rewritten.extend(
        (
            PatternRelationSpec(
                "pmos_mirror",
                "tail_source",
                "right_of",
                min_gap_um=0.0,
                notes="Reference packing: keep PMOS mirror and tail device in one min-gap local row.",
                hard=True,
            ),
            PatternRelationSpec(
                "pmos_mirror",
                "tail_source",
                "overlap_y",
                notes="Reference packing: keep PMOS mirror and tail device row-overlapped.",
                hard=True,
            ),
            PatternRelationSpec(
                "input_pair",
                "resistor_ladder",
                "above",
                min_gap_um=0.0,
                notes="Reference packing: place the folded resistor motif directly above the input row in the right local column.",
                hard=True,
            ),
            PatternRelationSpec(
                "resistor_ladder",
                "load_pair",
                "above",
                min_gap_um=0.0,
                notes="Reference packing: place the load pair directly above the resistor motif.",
                hard=True,
            ),
            PatternRelationSpec(
                "resistor_ladder",
                "input_pair",
                "align_x",
                tolerance_um=1.0,
                notes="Reference packing: center-align the input row to the folded resistor motif.",
                hard=True,
            ),
            PatternRelationSpec(
                "resistor_ladder",
                "load_pair",
                "align_x",
                tolerance_um=1.0,
                notes="Reference packing: center-align the load row to the folded resistor motif.",
                hard=True,
            ),
        )
    )

    patterns: list[DevicePatternSpec] = []
    fixed_patterns: dict[str, str] = {}
    for pattern in spec.patterns:
        if pattern.name == "resistor_ladder":
            snake = tuple(candidate for candidate in pattern.candidates if str(candidate.name).startswith("r_ladder_snake_"))
            if snake:
                patterns.append(replace(pattern, candidates=snake))
                fixed_patterns[pattern.name] = snake[0].name
                continue
        patterns.append(pattern)

    pack_constraints = _reference_min_gap_pack_windows(spec.pack_constraints)
    refined = replace(
        spec,
        patterns=tuple(patterns),
        relations=tuple(rewritten),
        pack_constraints=pack_constraints,
        pcell_realization_groups=_placement_relevant_realization_groups(spec.pcell_realization_groups),
        notes=(
            spec.notes
            + "\nMacro refinement: ADC/PLL reference-style min-gap motif packing with hard local column alignment."
        ).strip(),
    )
    return (
        MacroRefinementCandidateSpec(
            "reference_min_gap_column_packing",
            refined,
            {pattern.name: (pattern.name,) for pattern in refined.patterns},
            notes="Pack BJT/resistor/MOS motifs near local rule minima and center-align the right device column.",
            metadata={
                "kind": "reference_min_gap_packing",
                "reference_basis": "ADC/PLL reference layouts use finite primitive arrays, near-min local gaps, and simple row/column alignment.",
                "fixed_relations": fixed_relations,
                "fixed_patterns": fixed_patterns,
                "pcell_realization_policy": "preserve_mos_and_calibrated_or_drawn_passive_compact_realizations",
            },
        ),
    )


def bandgap_split_top_mos_void_insertion_candidates(
    spec: AnalogLayoutSpec,
    graph: TopologyGraph,
) -> tuple[MacroRefinementCandidateSpec, ...]:
    """Combine resistor-row splitting with the top-MOS void insertion strategy."""

    pattern = _pattern_by_name(spec).get("resistor_ladder")
    if pattern is None:
        return ()
    resistors = tuple(pattern.devices)
    if "R1" not in resistors or not any(name.startswith("R2_") for name in resistors):
        return ()
    ordered = tuple(sorted(resistors, key=_bandgap_resistor_order_key))
    split = _split_resistor_ladder_into_rows(spec, ordered, rows=3)
    if split is None:
        return ()
    row_names = tuple(str(name) for name in split.macro_subpatterns.get("resistor_ladder", ()))
    if len(row_names) < 2:
        return ()

    compact_relation_choices = {
        ("tail_source", "input_pair"): "left_of",  # DSL semantics: source right of target.
        ("input_pair", "load_pair"): "above",
        ("input_pair", "pmos_mirror"): "above",
        ("bjt_core", "input_pair"): "above",
        ("bjt_core", "tail_source"): "above",
    }
    gap_overrides = {
        ("bjt_core", row_names[0]): 0.5,
        (row_names[-1], "tail_source"): 0.0,
        (row_names[-1], "input_pair"): 0.0,
        ("input_pair", "load_pair"): 0.0,
        ("input_pair", "pmos_mirror"): 0.5,
    }
    rewritten: list[PatternRelationSpec] = []
    fixed_relations: dict[str, str] = {}
    applied_gaps: dict[str, float] = {}
    for relation in split.spec.relations:
        key = (relation.source, relation.target)
        selected_kind = compact_relation_choices.get(key)
        candidates = tuple(str(item).lower() for item in relation.candidates)
        updated = relation
        if selected_kind is not None and selected_kind in candidates:
            updated = replace(
                updated,
                kind=selected_kind,
                candidates=(),
                candidate_costs={},
                notes=(
                    updated.notes
                    + f" Macro refinement: fix {relation.source}->{relation.target}={selected_kind} for split top-MOS packing."
                ).strip(),
            )
            fixed_relations[f"{relation.source}->{relation.target}"] = selected_kind
        if key in gap_overrides:
            updated = replace(
                updated,
                min_gap_um=float(gap_overrides[key]),
                notes=(
                    updated.notes
                    + f" Macro refinement: reduce min_gap to {float(gap_overrides[key]):g}um for split top-MOS packing."
                ).strip(),
            )
            applied_gaps[f"{relation.source}->{relation.target}"] = float(gap_overrides[key])
        rewritten.append(updated)
    if fixed_relations.get("tail_source->input_pair") != "left_of" or not applied_gaps:
        return ()

    refined = replace(
        split.spec,
        relations=tuple(rewritten),
        pack_constraints=_bandgap_top_mos_void_insertion_pack_windows(
            split.spec.pack_constraints,
            ladder_patterns=row_names,
        ),
        pcell_realization_groups=_placement_relevant_realization_groups(split.spec.pcell_realization_groups),
        notes=(
            split.spec.notes
            + "\nMacro refinement: combine resistor row split with top MOS void insertion."
        ).strip(),
    )
    return (
        MacroRefinementCandidateSpec(
            "split_top_mos_tail_void_insertion",
            refined,
            split.macro_subpatterns,
            notes="Split resistor_ladder into compact rows and insert tail_source into the top MOS whitespace.",
            metadata={
                "kind": "split_void_insertion_refinement",
                "macro": "resistor_ladder",
                "row_count": len(row_names),
                "rows": dict(split.metadata.get("rows", {}) if split.metadata else {}),
                "inserted_macro": "tail_source",
                "fixed_relations": fixed_relations,
                "min_gap_um_overrides": applied_gaps,
                "pcell_realization_policy": "preserve_mos_and_calibrated_or_drawn_passive_compact_realizations",
            },
        ),
    )


def _placement_relevant_realization_groups(
    groups: Sequence[PCellRealizationGroupSpec],
) -> tuple[PCellRealizationGroupSpec, ...]:
    """Keep realization groups that change active-device placement envelopes.

    Top-level macro refinements must not blindly clear MOS PCell realization
    choices: before flat SMT, CRN28 MOS estimates can be tall/narrow, while a
    legal nf/m realization is short/wide and is required for compact packing.
    Unmarked native passive aspect alternatives are filtered out here because
    they still need aspect-specific terminal/access calibration.  Native
    passive candidates that come from calibration or explicit PDK PCell params
    are real placement envelopes and are preserved.  Drawn passive primitives
    are geometry-owned by this flow, so safe aspect candidates can remain in
    the main SMT for compactness experiments.
    """

    result: list[PCellRealizationGroupSpec] = []
    for group in tuple(groups or ()):
        if not _looks_like_passive_realization_group(group):
            result.append(_compact_active_realization_group(group))
            continue
        compact_group = _passive_compact_realization_group(group)
        if compact_group is not None:
            result.append(compact_group)
    return tuple(result)


def _compact_active_realization_group(group: PCellRealizationGroupSpec) -> PCellRealizationGroupSpec:
    candidates = tuple(group.candidates)
    if len(candidates) <= 1:
        return group
    selected = min(
        candidates,
        key=lambda candidate: (
            max(float(candidate.width_um), float(candidate.height_um)),
            float(candidate.width_um) * float(candidate.height_um),
            max(0, int(candidate.cost)),
        ),
    )
    return replace(
        group,
        candidates=(selected,),
        notes=(
            group.notes
            + " Macro refinement: keep most compact active-device realization for observation-driven packing."
        ).strip(),
    )


def _passive_compact_realization_group(group: PCellRealizationGroupSpec) -> PCellRealizationGroupSpec | None:
    relevant_candidates = tuple(
        candidate
        for candidate in tuple(group.candidates)
        if _is_drawn_passive_candidate(candidate) or _is_native_calibrated_or_configured_passive_candidate(candidate)
    )
    if not relevant_candidates:
        return None
    compact = tuple(
        candidate
        for candidate in relevant_candidates
        if "width_trim" in str(candidate.name).lower()
        or "compact" in str(candidate.name).lower()
    )
    selected = compact or relevant_candidates
    return replace(
        group,
        candidates=selected,
        notes=(
            group.notes
            + " Macro refinement: keep drawn or calibrated/configured native passive compact candidates for observation-driven packing."
        ).strip(),
    )


def _is_drawn_passive_candidate(candidate: PCellRealizationCandidateSpec) -> bool:
    try:
        sizing = dict(candidate.sizing_overrides or {})
    except Exception:
        return False
    return bool(sizing.get("use_drawn_primitive", sizing.get("use_drawn_passive_primitive", False)))


def _is_native_calibrated_or_configured_passive_candidate(candidate: PCellRealizationCandidateSpec) -> bool:
    try:
        sizing = dict(candidate.sizing_overrides or {})
    except Exception:
        return False
    if bool(sizing.get("use_drawn_primitive", sizing.get("use_drawn_passive_primitive", False))):
        return False
    if not bool(sizing.get("native_pcell_realization", False)):
        return False
    return (
        bool(sizing.get("calibrated_pcell_realization", False))
        or bool(sizing.get("configured_pcell_params", False))
        or bool(dict(candidate.pcell_overrides or {}))
    )


def _bandgap_top_mos_void_insertion_pack_windows(
    packs: Sequence[PackConstraintSpec],
    *,
    ladder_patterns: Sequence[str] = ("resistor_ladder",),
) -> tuple[PackConstraintSpec, ...]:
    """Add observation-derived local pack objectives for the top-MOS candidate.

    Broad hard max-width/max-height windows make the mixed soft-relation
    problem brittle under short z3 timeouts.  For this refinement we use one
    directional hard bound on the MOS-core width, because the observed failure
    mode is horizontal growth after tail insertion.  The other windows remain
    soft but high-priority.
    """

    updates = {
        "mos_core_local": {
            "patterns": ("tail_source", "input_pair", "pmos_mirror", "load_pair"),
            "max_width_um": 35.5,
            "weight": 32,
            "width_weight": 8,
            "height_weight": 16,
        },
        "bjt_res_input_local": {
            "patterns": ("bjt_core", *tuple(ladder_patterns), "input_pair", "tail_source"),
            "weight": 24,
            "width_weight": 8,
            "height_weight": 10,
        },
        "right_device_cluster": {
            "patterns": (*tuple(ladder_patterns), "tail_source", "input_pair", "pmos_mirror", "load_pair"),
            "weight": 36,
            "width_weight": 16,
            "height_weight": 8,
        },
    }
    seen: set[str] = set()
    rewritten: list[PackConstraintSpec] = []
    for pack in packs:
        update = updates.get(pack.name)
        if update is None:
            rewritten.append(pack)
            continue
        seen.add(pack.name)
        rewritten.append(
            replace(
                pack,
                patterns=tuple(update["patterns"]),  # type: ignore[arg-type]
                max_width_um=float(update["max_width_um"]) if "max_width_um" in update else None,
                max_height_um=None,
                weight=max(int(pack.weight), int(update["weight"])),
                width_weight=max(int(pack.width_weight), int(update["width_weight"])),
                height_weight=max(int(pack.height_weight), int(update["height_weight"])),
                notes=(
                    pack.notes
                    + " Macro refinement: observation-derived soft local pack objective for top MOS void insertion."
                ).strip(),
            )
        )
    for name, update in updates.items():
        if name in seen:
            continue
        rewritten.append(
            PackConstraintSpec(
                name,
                tuple(update["patterns"]),  # type: ignore[arg-type]
                float(update["max_width_um"]) if "max_width_um" in update else None,
                None,
                int(update["weight"]),
                int(update["width_weight"]),
                int(update["height_weight"]),
                0,
                "Observation-derived soft local pack objective for top MOS void insertion.",
            )
        )
    return tuple(rewritten)


def _reference_min_gap_pack_windows(
    packs: Sequence[PackConstraintSpec],
) -> tuple[PackConstraintSpec, ...]:
    updates = {
        "mos_core_local": {
            "patterns": ("pmos_mirror", "tail_source", "input_pair", "resistor_ladder", "load_pair"),
            "max_width_um": 20.0,
            "max_height_um": 33.0,
            "weight": 48,
            "width_weight": 18,
            "height_weight": 18,
        },
        "bjt_res_input_local": {
            "patterns": ("bjt_core", "resistor_ladder", "input_pair", "tail_source"),
            "max_width_um": 52.0,
            "max_height_um": 33.0,
            "weight": 36,
            "width_weight": 12,
            "height_weight": 16,
        },
        "right_device_cluster": {
            "patterns": ("pmos_mirror", "tail_source", "input_pair", "resistor_ladder", "load_pair"),
            "max_width_um": 20.0,
            "max_height_um": 33.0,
            "weight": 56,
            "width_weight": 20,
            "height_weight": 20,
        },
        "upper_device_cluster": {
            "patterns": ("resistor_ladder", "load_pair"),
            "max_width_um": 20.0,
            "weight": 32,
            "width_weight": 16,
            "height_weight": 6,
        },
        "pmos_load_local": {
            "patterns": ("pmos_mirror", "tail_source", "input_pair", "load_pair"),
            "max_width_um": 20.0,
            "max_height_um": 33.0,
            "weight": 32,
            "width_weight": 12,
            "height_weight": 12,
        },
    }
    seen: set[str] = set()
    rewritten: list[PackConstraintSpec] = []
    for pack in packs:
        update = updates.get(pack.name)
        if update is None:
            rewritten.append(pack)
            continue
        seen.add(pack.name)
        rewritten.append(
            replace(
                pack,
                patterns=tuple(update["patterns"]),  # type: ignore[arg-type]
                max_width_um=float(update["max_width_um"]) if "max_width_um" in update else pack.max_width_um,
                max_height_um=float(update["max_height_um"]) if "max_height_um" in update else pack.max_height_um,
                weight=max(int(pack.weight), int(update["weight"])),
                width_weight=max(int(pack.width_weight), int(update["width_weight"])),
                height_weight=max(int(pack.height_weight), int(update["height_weight"])),
                notes=(
                    pack.notes
                    + " Reference packing: near-min local motif window derived from ADC/PLL spacing observations."
                ).strip(),
            )
        )
    for name, update in updates.items():
        if name in seen:
            continue
        rewritten.append(
            PackConstraintSpec(
                name,
                tuple(update["patterns"]),  # type: ignore[arg-type]
                max_width_um=float(update["max_width_um"]) if "max_width_um" in update else None,
                max_height_um=float(update["max_height_um"]) if "max_height_um" in update else None,
                weight=int(update["weight"]),
                width_weight=int(update["width_weight"]),
                height_weight=int(update["height_weight"]),
                notes="Reference packing: near-min local motif window derived from ADC/PLL spacing observations.",
            )
        )
    return tuple(rewritten)


def aggregate_macro_bboxes_tracks(
    pattern_bboxes_tracks: Mapping[str, tuple[int, int, int, int]],
    macro_subpatterns: Mapping[str, Sequence[str]],
) -> dict[str, tuple[int, int, int, int]]:
    """Aggregate sub-pattern bboxes back to their original macro names."""

    result = {str(name): tuple(int(v) for v in bbox) for name, bbox in pattern_bboxes_tracks.items()}
    for macro, subpatterns in macro_subpatterns.items():
        boxes = [result[name] for name in subpatterns if name in result]
        if not boxes:
            continue
        result[str(macro)] = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
    return result


def _split_resistor_ladder_into_rows(
    spec: AnalogLayoutSpec,
    ordered_resistors: Sequence[str],
    *,
    rows: int,
) -> MacroRefinementCandidateSpec | None:
    rows = max(1, int(rows))
    if rows <= 1:
        return None
    chunks = _chunk_series_rows(tuple(ordered_resistors), rows)
    if len(chunks) <= 1:
        return None
    row_names = tuple(f"resistor_ladder_row{idx}" for idx in range(len(chunks)))
    row_patterns = tuple(
        DevicePatternSpec(
            row_name,
            "resistor_ladder",
            tuple(chunk),
            "row",
            (),
            0.5,
            0.0,
            "",
            "R0",
            f"Refined resistor ladder row {idx}; preserves local series order with no duplicated row margin.",
        )
        for idx, (row_name, chunk) in enumerate(zip(row_names, chunks))
    )
    patterns = tuple(pattern for pattern in spec.patterns if pattern.name != "resistor_ladder") + row_patterns
    relations = _rewrite_ladder_relations_for_rows(spec.relations, row_names)
    packs = _rewrite_ladder_packs_for_rows(spec.pack_constraints, row_names)
    objective_terms = _rewrite_ladder_objective_terms_for_rows(spec.objective_terms, row_names)
    refined = replace(
        spec,
        patterns=patterns,
        relations=relations,
        pack_constraints=packs,
        objective_terms=objective_terms,
        notes=(spec.notes + f"\nMacro refinement: resistor_ladder split into {len(row_names)} rows.").strip(),
    )
    return MacroRefinementCandidateSpec(
        f"resistor_ladder_split_{len(row_names)}rows",
        refined,
        {
            **{pattern.name: (pattern.name,) for pattern in refined.patterns},
            "resistor_ladder": row_names,
        },
        notes="Split resistor_ladder into row sub-patterns while preserving series order.",
        metadata={
            "kind": "series_ladder_split",
            "macro": "resistor_ladder",
            "row_count": len(row_names),
            "rows": {name: tuple(chunk) for name, chunk in zip(row_names, chunks)},
        },
    )


def _rewrite_ladder_relations_for_rows(
    relations: Sequence[PatternRelationSpec],
    row_names: Sequence[str],
) -> tuple[PatternRelationSpec, ...]:
    if not row_names:
        return tuple(relations)
    bottom = str(row_names[0])
    top = str(row_names[-1])
    rewritten: list[PatternRelationSpec] = []
    for relation in relations:
        source_is_ladder = relation.source == "resistor_ladder"
        target_is_ladder = relation.target == "resistor_ladder"
        if not source_is_ladder and not target_is_ladder:
            rewritten.append(relation)
            continue
        if source_is_ladder and target_is_ladder:
            continue
        if source_is_ladder:
            replacement_sources = tuple(row_names) if relation.kind in {"overlap_x", "overlap_y"} else (top,)
            for source in replacement_sources:
                rewritten.append(replace(relation, source=str(source)))
        else:
            replacement_targets = tuple(row_names) if relation.kind in {"overlap_x", "overlap_y"} else (bottom,)
            for target in replacement_targets:
                rewritten.append(replace(relation, target=str(target)))

    # Preserve the electrical series-row order as a compact vertical stack.
    for lower, upper in zip(row_names, row_names[1:]):
        rewritten.append(
            PatternRelationSpec(
                str(lower),
                str(upper),
                "above",
                0.5,
                0.0,
                "Series ladder row order: next row above previous row.",
                True,
                1,
                (),
                {},
            )
        )
        rewritten.append(
            PatternRelationSpec(
                str(lower),
                str(upper),
                "overlap_x",
                0.0,
                0.0,
                "Series ladder row order: keep folded row columns aligned.",
                True,
                1,
                (),
                {},
            )
        )
    return tuple(rewritten)


def _rewrite_ladder_packs_for_rows(
    packs: Sequence[PackConstraintSpec],
    row_names: Sequence[str],
) -> tuple[PackConstraintSpec, ...]:
    rewritten: list[PackConstraintSpec] = []
    for pack in packs:
        patterns: list[str] = []
        changed = False
        for pattern in pack.patterns:
            if pattern == "resistor_ladder":
                patterns.extend(str(name) for name in row_names)
                changed = True
            else:
                patterns.append(pattern)
        rewritten.append(replace(pack, patterns=tuple(dict.fromkeys(patterns))) if changed else pack)
    return tuple(rewritten)


def _rewrite_ladder_objective_terms_for_rows(
    objective_terms: Sequence[LayoutObjectiveTermSpec],
    row_names: Sequence[str],
) -> tuple[LayoutObjectiveTermSpec, ...]:
    """Keep SMT-visible layout-quality objectives alive after macro splitting.

    The compiler intentionally consumes only concrete pattern names.  Once a
    macro such as ``resistor_ladder`` is split into row sub-patterns, objective
    terms that still point at the old macro would be silently ignored.  Rewriting
    the pattern list here keeps the DSL's original intent visible to SMT without
    adding a second macro-alias mechanism to the compiler.
    """

    rewritten: list[LayoutObjectiveTermSpec] = []
    for term in objective_terms:
        if not _objective_term_supports_subpattern_expansion(term):
            rewritten.append(term)
            continue
        patterns: list[str] = []
        changed = False
        for pattern in term.patterns:
            if pattern == "resistor_ladder":
                patterns.extend(str(name) for name in row_names)
                changed = True
            else:
                patterns.append(pattern)
        rewritten.append(replace(term, patterns=tuple(dict.fromkeys(patterns))) if changed else term)
    return tuple(rewritten)


def _objective_term_supports_subpattern_expansion(term: LayoutObjectiveTermSpec) -> bool:
    kind = str(term.kind or "").lower()
    return kind in {
        "compact",
        "compact_envelope",
        "local_envelope",
        "void",
        "whitespace",
        "minimize_gap",
        "aesthetic_squareness",
        "squareness",
        "square_bbox",
        "bbox_aspect",
    }


def _chunk_series_rows(devices: tuple[str, ...], rows: int) -> tuple[tuple[str, ...], ...]:
    if rows <= 1:
        return (devices,)
    chunk_size = max(1, int(ceil(len(devices) / rows)))
    chunks: list[tuple[str, ...]] = []
    index = 0
    for row in range(rows):
        chunk = tuple(devices[index : index + chunk_size])
        index += chunk_size
        if not chunk:
            continue
        # Serpentine physical order keeps adjacent series terminals close.
        chunks.append(tuple(reversed(chunk)) if row % 2 else chunk)
    if index < len(devices):
        chunks[-1] = chunks[-1] + tuple(devices[index:])
    return tuple(chunks)


def _pattern_by_name(spec: AnalogLayoutSpec) -> dict[str, DevicePatternSpec]:
    return {pattern.name: pattern for pattern in spec.patterns}


def _looks_like_passive_realization_group(group: PCellRealizationGroupSpec) -> bool:
    haystack = " ".join(
        (
            str(group.name).lower(),
            str(group.notes).lower(),
            " ".join(str(candidate.name).lower() for candidate in group.candidates),
        )
    )
    return "passive" in haystack or "resistor" in haystack or "capacitor" in haystack


def _is_current_passive_candidate(candidate: PCellRealizationCandidateSpec) -> bool:
    name = str(candidate.name).lower()
    return name.endswith("_current") or name == "current"


def _bandgap_resistor_order_key(name: str) -> tuple[int, int, str]:
    if name == "R1":
        return (0, -1, name)
    if name.startswith("R2_"):
        try:
            return (1, int(name.rsplit("_", 1)[1]), name)
        except (IndexError, ValueError):
            return (1, 10_000, name)
    return (2, 10_000, name)
