"""Python DSL for constrained analog layout strategy.

The DSL is deliberately small: it captures the part of layout knowledge that
should bound an SMT solve (patterns, symmetry, critical nets, and compactness
objectives) while leaving ordinary routing and local DRC ECO to downstream
tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence


@dataclass(frozen=True)
class PatternCandidateSpec:
    """One concrete packing candidate for a device pattern."""

    name: str
    rows: int
    cols: int
    order: tuple[str, ...] = ()
    spacing_um: float | None = None
    margin_um: float | None = None
    cost: int = 0


@dataclass(frozen=True)
class PCellRealizationCandidateSpec:
    """One calibrated/estimated physical realization for one or more devices.

    The electrical sizing remains owned by the sizing stage.  These candidates
    describe layout-equivalent PCell choices such as different MOS nf/m splits
    or passive aspect-ratio implementations that the placement SMT may choose
    from.  ``sizing_overrides`` are written back into the final sizing map after
    SMT selection so PCell generation uses the same geometry seen by placement.
    """

    name: str
    width_um: float
    height_um: float
    sizing_overrides: Mapping[str, object] = field(default_factory=dict)
    pcell_overrides: Mapping[str, object] = field(default_factory=dict)
    cost: int = 0
    drc_clean: bool = True
    lvs_clean: bool = True
    notes: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PCellRealizationGroupSpec:
    """A set of devices that must share one PCell realization choice."""

    name: str
    devices: tuple[str, ...]
    candidates: tuple[PCellRealizationCandidateSpec, ...]
    require_same: bool = True
    notes: str = ""


@dataclass(frozen=True)
class DevicePatternSpec:
    """A compact placement macro controlled by the SMT solver as one box."""

    name: str
    role: str
    devices: tuple[str, ...]
    kind: str = "row"
    candidates: tuple[PatternCandidateSpec, ...] = ()
    spacing_um: float = 0.5
    margin_um: float = 0.25
    center_device: str = ""
    orient: str = "R0"
    notes: str = ""


@dataclass(frozen=True)
class PairConstraintSpec:
    """Device-level pair constraint inside a pattern."""

    name: str
    left: str
    right: str
    role: str = ""
    spacing_um: float | None = None
    mirror_right: bool = True
    same_y: bool = True
    notes: str = ""
    shared_sd: bool = False
    shared_sd_net: str = ""
    shared_sd_role: str = ""
    shared_sd_spacing_um: float | None = None
    shared_sd_weight: int = 0
    shared_sd_readiness: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PatternRelationSpec:
    """Relationship between compact pattern boxes.

    ``hard=True`` keeps the legacy behavior: the relation is asserted as an SMT
    constraint.  ``hard=False`` turns it into an objective penalty.  When
    ``candidates`` is non-empty, the solver chooses exactly one candidate
    relation and records the choice in the compile checks.
    """

    source: str
    target: str
    kind: str
    min_gap_um: float = 0.0
    tolerance_um: float = 0.0
    notes: str = ""
    hard: bool = True
    weight: int = 1
    candidates: tuple[str, ...] = ()
    candidate_costs: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CriticalNetSpec:
    """Critical net that should affect the SMT objective."""

    name: str
    weight: int = 1
    route_in_smt: bool = True
    shield: bool = False
    width_um: float | None = None
    notes: str = ""


@dataclass(frozen=True)
class RouteResourceSpec:
    """Layer/lane resource intent for a routed net or net prefix.

    This is a factual resource contract consumed by the structured routing
    stage.  Placement SMT can serialize it for observation, and routing can use
    it to choose legal trunk resources before local ECO.
    """

    name: str
    match: str = "net"
    layer: str = ""
    allowed_layers: tuple[str, ...] = ()
    forbidden_layers: tuple[str, ...] = ()
    cyclic_layers: tuple[str, ...] = ()
    lane: int | None = None
    cyclic_lanes: tuple[int, ...] = ()
    avoid_nets: tuple[str, ...] = ()
    avoid_prefixes: tuple[str, ...] = ()
    style: str = ""
    channel_orientation: str = ""
    channel_side: str = ""
    channel_offset_um: float | None = None
    dogleg_side: str = ""
    dogleg_offset_um: float | None = None
    dogleg_offset_step_um: float | None = None
    terminal_escape_style: str = ""
    terminal_escape_um: float | None = None
    route_policy: Mapping[str, object] = field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class PackConstraintSpec:
    """Group-level compactness window for several pattern boxes.

    This is the DSL hook for layout knowledge such as "the resistor ladder and
    BJT core should form one compact local cluster" without encoding their
    exact pairwise topology.  ``max_*`` values are hard constraints when set;
    weights add a local-envelope term to the SMT objective.
    """

    name: str
    patterns: tuple[str, ...]
    max_width_um: float | None = None
    max_height_um: float | None = None
    weight: int = 1
    width_weight: int = 1
    height_weight: int = 1
    area_weight: int = 0
    notes: str = ""


@dataclass(frozen=True)
class PlacementWindowSpec:
    """Fine-grained SMT-domain placement handle for one pattern box.

    Track coordinates refer to the pattern origin, not the device bbox union.
    Hard windows constrain the SMT feasible region.  Soft windows/targets add a
    local objective penalty and are intended for agent micro-adjustments from a
    factual observation artifact.
    """

    name: str
    pattern: str
    min_x_tracks: int | None = None
    max_x_tracks: int | None = None
    min_y_tracks: int | None = None
    max_y_tracks: int | None = None
    target_x_tracks: int | None = None
    target_y_tracks: int | None = None
    weight: int = 1
    hard: bool = False
    notes: str = ""


@dataclass(frozen=True)
class LayoutObjectiveTermSpec:
    """A soft layout-quality term consumed by the SMT objective.

    This is intentionally generic and small.  It lets a spec say what should be
    optimized (compact local envelope, edge alignment, center alignment, etc.)
    without turning those preferences into hard placement relations.
    """

    name: str
    kind: str
    patterns: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()
    weight: int = 1
    axis: str = "both"
    metric: str = ""
    target: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ObjectiveSpec:
    """Weights for the compact SMT objective."""

    bbox_weight: int = 100
    width_weight: int = 5
    height_weight: int = 3
    area_weight: int = 0
    true_area_weight: int = 0
    max_side_weight: int = 0
    hpwl_weight: int = 10
    right_whitespace_weight: int = 2
    aspect_weight: int = 1
    aspect_num: int = 1
    aspect_den: int = 1
    objective_term_weight: int = 1
    realization_weight: int = 1


@dataclass(frozen=True)
class DrcPolicySpec:
    """Design-rule knobs consumed by the SMT compiler.

    Values are intentionally high level.  The compiler resolves defaults from
    the active PDK/hierarchical rule metadata where possible.
    """

    rule_profile_path: str = ""
    placement_spacing_um: float | None = None
    pair_spacing_um_by_role: Mapping[str, float] = field(default_factory=dict)
    grid_um: float | None = None
    local_eco_rule_prefixes: tuple[str, ...] = ("M", "VIA", "CO", "PO")
    promote_to_smt_rule_prefixes: tuple[str, ...] = ("OD", "NW", "DNW", "DOD", "DPO")
    # A block-level guard ring is an outer floorplan envelope.  These values
    # are populated from the PDK configuration by ``analog_smt_flow``; keeping
    # them in the DSL makes the physical resource visible to every solver
    # candidate instead of adding an unaccounted ring after placement.
    guard_ring_enabled: bool = False
    guard_ring_net: str = "VSS"
    guard_ring_kind: str = "substrate"
    guard_ring_width_um: float = 0.0
    guard_ring_spacing_um: float = 0.0
    guard_ring_contact_pitch_um: float = 1.0
    guard_ring_extra_spacing_um_by_side: Mapping[str, float] = field(default_factory=dict)
    # Native MOS PCells already contain foundry-defined edge dummy geometry.
    # The SMT flow treats that geometry as part of the measured PCell bbox and
    # uses this contract to reject/report candidates that do not carry the
    # configured dummy parameters.  It deliberately does not create extra
    # extracted MOS devices merely to draw dummies.
    matched_mos_dummy_policy: str = "none"
    matched_mos_dummy_required_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalogLayoutSpec:
    """Serializable strategy IR produced by the Python DSL."""

    block: str
    patterns: tuple[DevicePatternSpec, ...] = ()
    pairs: tuple[PairConstraintSpec, ...] = ()
    relations: tuple[PatternRelationSpec, ...] = ()
    critical_nets: tuple[CriticalNetSpec, ...] = ()
    route_resources: tuple[RouteResourceSpec, ...] = ()
    pack_constraints: tuple[PackConstraintSpec, ...] = ()
    placement_windows: tuple[PlacementWindowSpec, ...] = ()
    objective_terms: tuple[LayoutObjectiveTermSpec, ...] = ()
    pcell_realization_groups: tuple[PCellRealizationGroupSpec, ...] = ()
    noncritical_router: str = "astar"
    objective: ObjectiveSpec = field(default_factory=ObjectiveSpec)
    drc: DrcPolicySpec = field(default_factory=DrcPolicySpec)
    notes: str = ""

    def pattern_by_name(self) -> dict[str, DevicePatternSpec]:
        return {item.name: item for item in self.patterns}

    def device_to_pattern(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for pattern in self.patterns:
            for dev in pattern.devices:
                result[str(dev)] = pattern.name
        return result


class AnalogLayoutSpecBuilder:
    """Fluent builder for :class:`AnalogLayoutSpec`."""

    def __init__(self, block: str) -> None:
        self._block = str(block)
        self._patterns: list[DevicePatternSpec] = []
        self._pairs: list[PairConstraintSpec] = []
        self._relations: list[PatternRelationSpec] = []
        self._critical_nets: list[CriticalNetSpec] = []
        self._route_resources: list[RouteResourceSpec] = []
        self._pack_constraints: list[PackConstraintSpec] = []
        self._placement_windows: list[PlacementWindowSpec] = []
        self._objective_terms: list[LayoutObjectiveTermSpec] = []
        self._pcell_realization_groups: list[PCellRealizationGroupSpec] = []
        self._objective = ObjectiveSpec()
        self._drc = DrcPolicySpec()
        self._noncritical_router = "astar"
        self._notes = ""

    def pattern(
        self,
        name: str,
        devices: Sequence[str],
        *,
        role: str | None = None,
        kind: str = "row",
        candidates: Sequence[PatternCandidateSpec] = (),
        spacing_um: float = 0.5,
        margin_um: float = 0.25,
        center_device: str = "",
        orient: str = "R0",
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        self._patterns.append(
            DevicePatternSpec(
                str(name),
                str(role or name),
                tuple(str(dev) for dev in devices),
                str(kind),
                tuple(candidates),
                float(spacing_um),
                float(margin_um),
                str(center_device),
                str(orient),
                str(notes),
            )
        )
        return self

    def pair(
        self,
        name: str,
        left: str,
        right: str,
        *,
        role: str = "",
        spacing_um: float | None = None,
        mirror_right: bool = True,
        same_y: bool = True,
        notes: str = "",
        shared_sd: bool = False,
        shared_sd_net: str = "",
        shared_sd_role: str = "",
        shared_sd_spacing_um: float | None = None,
        shared_sd_weight: int = 0,
        shared_sd_readiness: Mapping[str, object] | None = None,
    ) -> "AnalogLayoutSpecBuilder":
        self._pairs.append(
            PairConstraintSpec(
                name=str(name),
                left=str(left),
                right=str(right),
                role=str(role),
                spacing_um=None if spacing_um is None else float(spacing_um),
                mirror_right=bool(mirror_right),
                same_y=bool(same_y),
                notes=str(notes),
                shared_sd=bool(shared_sd),
                shared_sd_net=str(shared_sd_net),
                shared_sd_role=str(shared_sd_role),
                shared_sd_spacing_um=None if shared_sd_spacing_um is None else float(shared_sd_spacing_um),
                shared_sd_weight=int(shared_sd_weight),
                shared_sd_readiness={str(key): value for key, value in dict(shared_sd_readiness or {}).items()},
            )
        )
        return self

    def relation(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        min_gap_um: float = 0.0,
        tolerance_um: float = 0.0,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        self._relations.append(
            PatternRelationSpec(
                str(source),
                str(target),
                str(kind),
                float(min_gap_um),
                float(tolerance_um),
                str(notes),
                True,
                1,
                (),
                {},
            )
        )
        return self

    def soft_relation(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        min_gap_um: float = 0.0,
        tolerance_um: float = 0.0,
        weight: int = 1,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        self._relations.append(
            PatternRelationSpec(
                str(source),
                str(target),
                str(kind),
                float(min_gap_um),
                float(tolerance_um),
                str(notes),
                False,
                int(weight),
                (),
                {},
            )
        )
        return self

    def choose_relation(
        self,
        source: str,
        target: str,
        candidates: Sequence[str],
        *,
        min_gap_um: float = 0.0,
        tolerance_um: float = 0.0,
        hard: bool = True,
        weight: int = 1,
        candidate_costs: Mapping[str, int] | None = None,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        normalized = tuple(str(item) for item in candidates if str(item))
        if not normalized:
            raise ValueError("choose_relation requires at least one candidate relation kind")
        self._relations.append(
            PatternRelationSpec(
                str(source),
                str(target),
                normalized[0],
                float(min_gap_um),
                float(tolerance_um),
                str(notes),
                bool(hard),
                int(weight),
                normalized,
                {str(key).lower(): int(value) for key, value in dict(candidate_costs or {}).items()},
            )
        )
        return self

    def pack(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        max_width_um: float | None = None,
        max_height_um: float | None = None,
        weight: int = 1,
        width_weight: int = 1,
        height_weight: int = 1,
        area_weight: int = 0,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        normalized = tuple(dict.fromkeys(str(pattern) for pattern in patterns if str(pattern)))
        if not normalized:
            raise ValueError("pack requires at least one pattern name")
        self._pack_constraints.append(
            PackConstraintSpec(
                str(name),
                normalized,
                None if max_width_um is None else float(max_width_um),
                None if max_height_um is None else float(max_height_um),
                int(weight),
                int(width_weight),
                int(height_weight),
                int(area_weight),
                str(notes),
            )
        )
        return self

    def objective_term(
        self,
        name: str,
        kind: str,
        *,
        patterns: Sequence[str] = (),
        devices: Sequence[str] = (),
        weight: int = 1,
        axis: str = "both",
        metric: str = "",
        target: str = "",
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        normalized_patterns = tuple(dict.fromkeys(str(pattern) for pattern in patterns if str(pattern)))
        normalized_devices = tuple(dict.fromkeys(str(device) for device in devices if str(device)))
        if not normalized_patterns and not normalized_devices:
            raise ValueError("objective_term requires at least one pattern or device")
        self._objective_terms.append(
            LayoutObjectiveTermSpec(
                str(name),
                str(kind),
                normalized_patterns,
                normalized_devices,
                int(weight),
                str(axis or "both").lower(),
                str(metric),
                str(target),
                str(notes),
            )
        )
        return self

    def placement_window(
        self,
        name: str,
        pattern: str,
        *,
        min_x_tracks: int | None = None,
        max_x_tracks: int | None = None,
        min_y_tracks: int | None = None,
        max_y_tracks: int | None = None,
        target_x_tracks: int | None = None,
        target_y_tracks: int | None = None,
        weight: int = 1,
        hard: bool = False,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add a fine-grained placement target/window for one pattern box."""

        if not str(pattern):
            raise ValueError("placement_window requires a pattern name")
        self._placement_windows.append(
            PlacementWindowSpec(
                str(name),
                str(pattern),
                None if min_x_tracks is None else int(min_x_tracks),
                None if max_x_tracks is None else int(max_x_tracks),
                None if min_y_tracks is None else int(min_y_tracks),
                None if max_y_tracks is None else int(max_y_tracks),
                None if target_x_tracks is None else int(target_x_tracks),
                None if target_y_tracks is None else int(target_y_tracks),
                int(weight),
                bool(hard),
                str(notes),
            )
        )
        return self

    def compact_objective(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        weight: int = 1,
        axis: str = "both",
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add a soft local-envelope compaction objective."""

        return self.objective_term(
            name,
            "compact_envelope",
            patterns=patterns,
            weight=weight,
            axis=axis,
            notes=notes,
        )

    def align_edges(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        axis: str = "both",
        weight: int = 1,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add a soft edge-alignment objective among pattern boxes."""

        return self.objective_term(
            name,
            "edge_alignment",
            patterns=patterns,
            weight=weight,
            axis=axis,
            notes=notes,
        )

    def align_centers(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        axis: str = "both",
        weight: int = 1,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add a soft centerline-alignment objective among pattern boxes."""

        return self.objective_term(
            name,
            "center_alignment",
            patterns=patterns,
            weight=weight,
            axis=axis,
            notes=notes,
        )

    def squareness_objective(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        weight: int = 1,
        target_aspect: str = "1:1",
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add a soft square/aspect objective for a group envelope."""

        return self.objective_term(
            name,
            "aesthetic_squareness",
            patterns=patterns,
            weight=weight,
            target=target_aspect,
            notes=notes,
        )

    def mirror_symmetry(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        axis: str = "x",
        weight: int = 1,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add a soft mirror-symmetry objective among ordered pattern boxes.

        For more than two patterns, the first pattern is paired with the last,
        the second with the second-last, and so on.
        """

        return self.objective_term(
            name,
            "mirror_symmetry",
            patterns=patterns,
            weight=weight,
            axis=axis,
            notes=notes,
        )

    def regular_spacing(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        axis: str = "x",
        weight: int = 1,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add a soft equal-spacing objective for ordered pattern boxes."""

        return self.objective_term(
            name,
            "regular_spacing",
            patterns=patterns,
            weight=weight,
            axis=axis,
            notes=notes,
        )

    def aesthetic_objectives(
        self,
        name: str,
        patterns: Sequence[str],
        *,
        squareness_weight: int = 1,
        compactness_weight: int = 1,
        alignment_weight: int = 1,
        regularity_weight: int = 1,
        target_aspect: str = "1:1",
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Add the default SMT-visible block aesthetics surrogate terms."""

        normalized = tuple(dict.fromkeys(str(pattern) for pattern in patterns if str(pattern)))
        if not normalized:
            raise ValueError("aesthetic_objectives requires at least one pattern name")
        if squareness_weight > 0:
            self.squareness_objective(
                f"{name}_squareness",
                normalized,
                weight=squareness_weight,
                target_aspect=target_aspect,
                notes=notes,
            )
        if compactness_weight > 0:
            self.compact_objective(
                f"{name}_compact_envelope",
                normalized,
                weight=compactness_weight,
                notes=notes,
            )
        if alignment_weight > 0 and len(normalized) >= 2:
            self.align_edges(
                f"{name}_edge_alignment",
                normalized,
                axis="both",
                weight=alignment_weight,
                notes=notes,
            )
        if regularity_weight > 0 and len(normalized) >= 3:
            self.regular_spacing(
                f"{name}_regular_spacing_x",
                normalized,
                axis="x",
                weight=regularity_weight,
                notes=notes,
            )
            self.regular_spacing(
                f"{name}_regular_spacing_y",
                normalized,
                axis="y",
                weight=regularity_weight,
                notes=notes,
            )
        return self

    def pcell_realization_group(
        self,
        name: str,
        devices: Sequence[str],
        candidates: Sequence[PCellRealizationCandidateSpec],
        *,
        require_same: bool = True,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        normalized_devices = tuple(dict.fromkeys(str(dev) for dev in devices if str(dev)))
        normalized_candidates = tuple(candidates)
        if not normalized_devices:
            raise ValueError("pcell_realization_group requires at least one device")
        if not normalized_candidates:
            raise ValueError("pcell_realization_group requires at least one candidate")
        self._pcell_realization_groups.append(
            PCellRealizationGroupSpec(
                str(name),
                normalized_devices,
                normalized_candidates,
                bool(require_same),
                str(notes),
            )
        )
        return self

    def realization_group(
        self,
        name: str,
        devices: Sequence[str],
        candidates: Sequence[PCellRealizationCandidateSpec],
        *,
        require_same: bool = True,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        """Alias for ``pcell_realization_group`` for compact DSL specs."""

        return self.pcell_realization_group(
            name,
            devices,
            candidates,
            require_same=require_same,
            notes=notes,
        )

    def critical_net(
        self,
        name: str,
        *,
        weight: int = 1,
        route_in_smt: bool = True,
        shield: bool = False,
        width_um: float | None = None,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        self._critical_nets.append(
            CriticalNetSpec(
                str(name),
                int(weight),
                bool(route_in_smt),
                bool(shield),
                None if width_um is None else float(width_um),
                str(notes),
            )
        )
        return self

    def route_resource(
        self,
        name: str,
        *,
        match: str = "net",
        layer: str = "",
        allowed_layers: Sequence[str] = (),
        forbidden_layers: Sequence[str] = (),
        cyclic_layers: Sequence[str] = (),
        lane: int | None = None,
        cyclic_lanes: Sequence[int] = (),
        avoid_nets: Sequence[str] = (),
        avoid_prefixes: Sequence[str] = (),
        style: str = "",
        channel_orientation: str = "",
        channel_side: str = "",
        channel_offset_um: float | None = None,
        dogleg_side: str = "",
        dogleg_offset_um: float | None = None,
        dogleg_offset_step_um: float | None = None,
        terminal_escape_style: str = "",
        terminal_escape_um: float | None = None,
        route_policy: Mapping[str, object] | None = None,
        notes: str = "",
    ) -> "AnalogLayoutSpecBuilder":
        normalized_match = str(match or "net").lower()
        if normalized_match not in {"net", "prefix"}:
            raise ValueError("route_resource match must be 'net' or 'prefix'")
        self._route_resources.append(
            RouteResourceSpec(
                str(name),
                normalized_match,
                str(layer),
                tuple(str(item) for item in allowed_layers if str(item)),
                tuple(str(item) for item in forbidden_layers if str(item)),
                tuple(str(item) for item in cyclic_layers if str(item)),
                None if lane is None else int(lane),
                tuple(int(item) for item in cyclic_lanes),
                tuple(str(item) for item in avoid_nets if str(item)),
                tuple(str(item) for item in avoid_prefixes if str(item)),
                str(style),
                str(channel_orientation),
                str(channel_side),
                None if channel_offset_um is None else float(channel_offset_um),
                str(dogleg_side),
                None if dogleg_offset_um is None else float(dogleg_offset_um),
                None if dogleg_offset_step_um is None else float(dogleg_offset_step_um),
                str(terminal_escape_style),
                None if terminal_escape_um is None else float(terminal_escape_um),
                {str(key): value for key, value in dict(route_policy or {}).items()},
                str(notes),
            )
        )
        return self

    def objective(self, **kwargs: object) -> "AnalogLayoutSpecBuilder":
        self._objective = replace(self._objective, **kwargs)
        return self

    def drc_policy(self, **kwargs: object) -> "AnalogLayoutSpecBuilder":
        self._drc = replace(self._drc, **kwargs)
        return self

    def noncritical_router(self, name: str) -> "AnalogLayoutSpecBuilder":
        self._noncritical_router = str(name)
        return self

    def notes(self, text: str) -> "AnalogLayoutSpecBuilder":
        self._notes = str(text)
        return self

    def build(self) -> AnalogLayoutSpec:
        return AnalogLayoutSpec(
            block=self._block,
            patterns=tuple(self._patterns),
            pairs=tuple(self._pairs),
            relations=tuple(self._relations),
            critical_nets=tuple(self._critical_nets),
            route_resources=tuple(self._route_resources),
            pack_constraints=tuple(self._pack_constraints),
            placement_windows=tuple(self._placement_windows),
            objective_terms=tuple(self._objective_terms),
            pcell_realization_groups=tuple(self._pcell_realization_groups),
            noncritical_router=self._noncritical_router,
            objective=self._objective,
            drc=self._drc,
            notes=self._notes,
        )


def layout_spec(block: str) -> AnalogLayoutSpecBuilder:
    return AnalogLayoutSpecBuilder(block)


def grid_candidate(
    name: str,
    rows: int,
    cols: int,
    *,
    order: Sequence[str] = (),
    spacing_um: float | None = None,
    margin_um: float | None = None,
    cost: int = 0,
) -> PatternCandidateSpec:
    return PatternCandidateSpec(
        str(name),
        max(1, int(rows)),
        max(1, int(cols)),
        tuple(str(dev) for dev in order),
        None if spacing_um is None else float(spacing_um),
        None if margin_um is None else float(margin_um),
        int(cost),
    )


def pcell_candidate(
    name: str,
    width_um: float,
    height_um: float,
    *,
    sizing_overrides: Mapping[str, object] | None = None,
    pcell_overrides: Mapping[str, object] | None = None,
    cost: int = 0,
    drc_clean: bool = True,
    lvs_clean: bool = True,
    notes: str = "",
    metadata: Mapping[str, object] | None = None,
) -> PCellRealizationCandidateSpec:
    return PCellRealizationCandidateSpec(
        str(name),
        max(float(width_um), 1e-6),
        max(float(height_um), 1e-6),
        {str(key): value for key, value in dict(sizing_overrides or {}).items()},
        {str(key): value for key, value in dict(pcell_overrides or {}).items()},
        int(cost),
        bool(drc_clean),
        bool(lvs_clean),
        str(notes),
        {str(key): value for key, value in dict(metadata or {}).items()},
    )
