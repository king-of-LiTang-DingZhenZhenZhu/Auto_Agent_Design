"""Serializable agent layout tweak patches.

The tweak layer is a narrow interface between factual layout observations and
the existing Python DSL/SMT compiler.  It records small, replayable operations
that an agent can propose after reading an observation artifact.

Only operations that can be mapped safely back into DSL/SMT inputs are applied
here.  This module deliberately avoids direct edits to generated geometry.
"""
from __future__ import annotations

import json
import fnmatch
import re
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .analog_layout_dsl import (
    AnalogLayoutSpec,
    DevicePatternSpec,
    LayoutObjectiveTermSpec,
    PackConstraintSpec,
    PatternCandidateSpec,
    PlacementWindowSpec,
    RouteResourceSpec,
)


SCHEMA_VERSION = "layout_tweak_patch/v1"


@dataclass(frozen=True)
class LayoutTweakOperation:
    """One fine-grained layout adjustment request.

    The fields are intentionally generic so the same patch schema can describe
    placement, pattern-realization, and routing-channel tweaks without creating
    a large DSL.
    """

    op: str
    target: str = ""
    source: str = ""
    target_group: str = ""
    axis: str = ""
    edge: str = ""
    dx_tracks: int | None = None
    dy_tracks: int | None = None
    dx_um: float | None = None
    dy_um: float | None = None
    min_x_tracks: int | None = None
    max_x_tracks: int | None = None
    min_y_tracks: int | None = None
    max_y_tracks: int | None = None
    target_x_tracks: int | None = None
    target_y_tracks: int | None = None
    window_margin_tracks: int | None = None
    spacing_um: float | None = None
    spacing_options_um: tuple[float, ...] = ()
    topology_options: tuple[str, ...] = ()
    layer: str = ""
    lane: int | None = None
    channel_side: str = ""
    route_name: str = ""
    solver: str = ""
    risk: str = ""
    hard: bool = False
    weight: int = 1
    observation_refs: tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutTweakPatch:
    """A replayable set of agent layout tweak operations."""

    patch_id: str
    baseline_layout_id: str = ""
    observation_refs: tuple[str, ...] = ()
    operations: tuple[LayoutTweakOperation, ...] = ()
    acceptance: Mapping[str, object] = field(default_factory=dict)
    notes: str = ""
    schema_version: str = SCHEMA_VERSION


class LayoutTweakPatchBuilder:
    """Fluent builder for :class:`LayoutTweakPatch`."""

    def __init__(self, patch_id: str, *, baseline_layout_id: str = "") -> None:
        self._patch_id = str(patch_id)
        self._baseline_layout_id = str(baseline_layout_id)
        self._observation_refs: list[str] = []
        self._operations: list[LayoutTweakOperation] = []
        self._acceptance: dict[str, object] = {}
        self._notes = ""

    def observation_refs(self, *refs: str) -> "LayoutTweakPatchBuilder":
        self._observation_refs.extend(str(ref) for ref in refs if str(ref))
        return self

    def nudge(
        self,
        target: str,
        *,
        dx_tracks: int = 0,
        dy_tracks: int = 0,
        dx_um: float | None = None,
        dy_um: float | None = None,
        axis: str = "",
        window_margin_tracks: int | None = 1,
        hard: bool = False,
        weight: int = 40,
        observation_refs: Sequence[str] = (),
        metadata: Mapping[str, object] | None = None,
        risk: str = "",
    ) -> "LayoutTweakPatchBuilder":
        self._operations.append(
            LayoutTweakOperation(
                "nudge",
                target=str(target),
                dx_tracks=int(dx_tracks),
                dy_tracks=int(dy_tracks),
                dx_um=None if dx_um is None else float(dx_um),
                dy_um=None if dy_um is None else float(dy_um),
                axis=str(axis or _axis_from_delta(dx_tracks, dy_tracks)),
                window_margin_tracks=None if window_margin_tracks is None else int(window_margin_tracks),
                hard=bool(hard),
                weight=int(weight),
                risk=str(risk),
                observation_refs=_unique_strings(observation_refs),
                metadata=dict(metadata or {}),
            )
        )
        return self

    def placement_window(
        self,
        target: str,
        *,
        min_x_tracks: int | None = None,
        max_x_tracks: int | None = None,
        min_y_tracks: int | None = None,
        max_y_tracks: int | None = None,
        target_x_tracks: int | None = None,
        target_y_tracks: int | None = None,
        hard: bool = False,
        weight: int = 40,
        observation_refs: Sequence[str] = (),
        risk: str = "",
    ) -> "LayoutTweakPatchBuilder":
        self._operations.append(
            LayoutTweakOperation(
                "placement_window",
                target=str(target),
                min_x_tracks=None if min_x_tracks is None else int(min_x_tracks),
                max_x_tracks=None if max_x_tracks is None else int(max_x_tracks),
                min_y_tracks=None if min_y_tracks is None else int(min_y_tracks),
                max_y_tracks=None if max_y_tracks is None else int(max_y_tracks),
                target_x_tracks=None if target_x_tracks is None else int(target_x_tracks),
                target_y_tracks=None if target_y_tracks is None else int(target_y_tracks),
                solver="global_smt",
                hard=bool(hard),
                weight=int(weight),
                risk=str(risk),
                observation_refs=_unique_strings(observation_refs),
            )
        )
        return self

    def compact_gap(
        self,
        source: str,
        target: str,
        *,
        axis: str = "both",
        solver: str = "global_smt",
        observation_refs: Sequence[str] = (),
        risk: str = "",
    ) -> "LayoutTweakPatchBuilder":
        self._operations.append(
            LayoutTweakOperation(
                "compact_gap",
                source=str(source),
                target=str(target),
                axis=str(axis or "both").lower(),
                solver=str(solver),
                risk=str(risk),
                observation_refs=_unique_strings(observation_refs),
            )
        )
        return self

    def align_edge(
        self,
        source: str,
        target: str,
        *,
        edge: str,
        observation_refs: Sequence[str] = (),
        risk: str = "",
    ) -> "LayoutTweakPatchBuilder":
        self._operations.append(
            LayoutTweakOperation(
                "align_edge",
                source=str(source),
                target=str(target),
                edge=str(edge),
                axis=_axis_from_edge(edge),
                solver="global_smt",
                risk=str(risk),
                observation_refs=_unique_strings(observation_refs),
            )
        )
        return self

    def pattern_candidate(
        self,
        target: str,
        *,
        spacing_um: float | None = None,
        spacing_options_um: Sequence[float] = (),
        topology_options: Sequence[str] = (),
        observation_refs: Sequence[str] = (),
        risk: str = "",
    ) -> "LayoutTweakPatchBuilder":
        self._operations.append(
            LayoutTweakOperation(
                "pattern_candidate",
                target=str(target),
                spacing_um=None if spacing_um is None else float(spacing_um),
                spacing_options_um=tuple(float(value) for value in spacing_options_um),
                topology_options=_unique_strings(topology_options),
                solver="global_smt",
                risk=str(risk),
                observation_refs=_unique_strings(observation_refs),
            )
        )
        return self

    def route_lane(
        self,
        route_name: str,
        *,
        match: str = "net",
        layer: str = "",
        lane: int | None = None,
        channel_side: str = "",
        style: str = "",
        channel_orientation: str = "",
        channel_offset_um: float | None = None,
        dogleg_side: str = "",
        dogleg_offset_um: float | None = None,
        terminal_escape_style: str = "",
        terminal_escape_um: float | None = None,
        route_policy: Mapping[str, object] | None = None,
        observation_refs: Sequence[str] = (),
        risk: str = "",
    ) -> "LayoutTweakPatchBuilder":
        metadata = {
            "match": str(match or "net").lower(),
            "style": str(style),
            "channel_orientation": str(channel_orientation),
            "channel_offset_um": None if channel_offset_um is None else float(channel_offset_um),
            "dogleg_side": str(dogleg_side),
            "dogleg_offset_um": None if dogleg_offset_um is None else float(dogleg_offset_um),
            "terminal_escape_style": str(terminal_escape_style),
            "terminal_escape_um": None if terminal_escape_um is None else float(terminal_escape_um),
            "route_policy": dict(route_policy or {}),
        }
        self._operations.append(
            LayoutTweakOperation(
                "route_lane",
                route_name=str(route_name),
                layer=str(layer),
                lane=None if lane is None else int(lane),
                channel_side=str(channel_side),
                solver="routing_eco",
                risk=str(risk),
                observation_refs=_unique_strings(observation_refs),
                metadata={key: value for key, value in metadata.items() if _metadata_value_present(value)},
            )
        )
        return self

    def acceptance(self, **criteria: object) -> "LayoutTweakPatchBuilder":
        self._acceptance.update({str(key): value for key, value in criteria.items()})
        return self

    def notes(self, text: str) -> "LayoutTweakPatchBuilder":
        self._notes = str(text)
        return self

    def build(self) -> LayoutTweakPatch:
        return LayoutTweakPatch(
            patch_id=self._patch_id,
            baseline_layout_id=self._baseline_layout_id,
            observation_refs=_unique_strings(self._observation_refs),
            operations=tuple(self._operations),
            acceptance=dict(self._acceptance),
            notes=self._notes,
        )


def layout_tweak(patch_id: str, *, baseline_layout_id: str = "") -> LayoutTweakPatchBuilder:
    """Start a replayable layout tweak patch."""

    return LayoutTweakPatchBuilder(patch_id, baseline_layout_id=baseline_layout_id)


def layout_tweak_patch_to_dict(patch: LayoutTweakPatch | Mapping[str, Any]) -> dict[str, Any]:
    """Convert a tweak patch into a stable JSON-friendly dictionary."""

    if isinstance(patch, Mapping):
        return _clean_json(dict(patch))
    return _clean_json(asdict(patch))


def layout_tweak_patch_from_dict(data: Mapping[str, Any]) -> LayoutTweakPatch:
    """Parse a JSON-style mapping into :class:`LayoutTweakPatch`."""

    operations = tuple(_operation_from_dict(row) for row in tuple(data.get("operations", ()) or ()))
    return LayoutTweakPatch(
        patch_id=str(data.get("patch_id", "")),
        baseline_layout_id=str(data.get("baseline_layout_id", "")),
        observation_refs=_unique_strings(tuple(data.get("observation_refs", ()) or ())),
        operations=operations,
        acceptance=dict(_mapping(data.get("acceptance"))),
        notes=str(data.get("notes", "")),
        schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
    )


def write_layout_tweak_patch_json(patch: LayoutTweakPatch | Mapping[str, Any], path: str | Path) -> Path:
    """Write a tweak patch as deterministic JSON."""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(layout_tweak_patch_to_dict(patch), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def layout_tweak_patch_with_operation_hardness(
    patch: LayoutTweakPatch | Mapping[str, Any],
    *,
    hard: bool,
    weight: int | None = None,
    operation_kinds: Sequence[str] = ("nudge", "placement_window"),
) -> LayoutTweakPatch:
    """Return a patch with selected operation kinds converted to hard/soft."""

    patch_obj = patch if isinstance(patch, LayoutTweakPatch) else layout_tweak_patch_from_dict(patch)
    selected_kinds = {str(kind).lower() for kind in operation_kinds}
    operations: list[LayoutTweakOperation] = []
    for operation in patch_obj.operations:
        if str(operation.op).lower() in selected_kinds:
            operations.append(
                replace(
                    operation,
                    hard=bool(hard),
                    weight=int(weight) if weight is not None else int(operation.weight),
                )
            )
        else:
            operations.append(operation)
    return replace(
        patch_obj,
        operations=tuple(operations),
        notes=_append_note(patch_obj.notes, f"Replay hardness override: hard={bool(hard)}."),
    )


def apply_layout_tweak_patch_to_spec(
    spec: AnalogLayoutSpec,
    patch: LayoutTweakPatch | Mapping[str, Any],
    *,
    observation: Mapping[str, Any] | None = None,
) -> AnalogLayoutSpec:
    """Apply the supported subset of a tweak patch to an analog layout spec.

    Supported now:
    - ``pattern_candidate``: narrows a pattern's internal spacing choice.
    - ``compact_gap``: adds/merges a local pack objective between two patterns.
    - ``align_edge``: adds/merges a soft edge-alignment objective.

    ``nudge`` and ``placement_window`` become SMT-domain placement windows when
    enough target/baseline coordinate facts are available.  ``route_lane`` is
    preserved in the patch artifact but is not applied by this global placement
    adapter.
    """

    patch_obj = patch if isinstance(patch, LayoutTweakPatch) else layout_tweak_patch_from_dict(patch)
    patterns = tuple(spec.patterns)
    packs = tuple(spec.pack_constraints)
    placement_windows = tuple(getattr(spec, "placement_windows", ()) or ())
    objective_terms = tuple(spec.objective_terms)
    route_resources = tuple(getattr(spec, "route_resources", ()) or ())
    applied: list[str] = []
    deferred: list[str] = []

    for operation in patch_obj.operations:
        op = str(operation.op).lower()
        if op == "pattern_candidate":
            patterns, changed = _apply_pattern_candidate(patterns, operation, patch_obj.patch_id)
            if changed:
                applied.append(f"pattern_candidate:{operation.target}")
            continue
        if op == "compact_gap":
            addition = _compact_gap_pack(operation, patch_obj.patch_id)
            if addition is not None:
                packs = _merge_pack_constraints(packs, (addition,))
                applied.append(f"compact_gap:{operation.source}->{operation.target}")
            continue
        if op == "align_edge":
            addition = _align_edge_objective(operation, patch_obj.patch_id)
            if addition is not None:
                objective_terms = _merge_objective_terms(objective_terms, (addition,))
                applied.append(f"align_edge:{operation.source}->{operation.target}")
            continue
        if op == "nudge":
            addition = _nudge_placement_window(operation, patch_obj.patch_id, observation=observation)
            if addition is not None:
                placement_windows = _merge_placement_windows(placement_windows, (addition,))
                applied.append(f"nudge:{operation.target}")
            else:
                deferred.append(op)
            continue
        if op == "placement_window":
            addition = _explicit_placement_window(operation, patch_obj.patch_id)
            if addition is not None:
                placement_windows = _merge_placement_windows(placement_windows, (addition,))
                applied.append(f"placement_window:{operation.target}")
            else:
                deferred.append(op)
            continue
        if op in {"route_lane"}:
            addition = _route_lane_resource(route_resources, operation, patch_obj.patch_id)
            if addition is not None:
                route_resources = _merge_route_resources(route_resources, (addition,))
                applied.append(f"route_lane:{addition.name}")
            else:
                deferred.append(op)
            continue
        deferred.append(op)

    notes = spec.notes
    if applied or deferred:
        suffix = f"Layout tweak patch {patch_obj.patch_id}: applied={tuple(applied)}"
        if deferred:
            suffix += f"; deferred={tuple(deferred)}"
        notes = (notes + "\n" + suffix).strip()

    return replace(
        spec,
        patterns=patterns,
        pack_constraints=packs,
        placement_windows=placement_windows,
        objective_terms=objective_terms,
        route_resources=route_resources,
        notes=notes,
    )


def layout_tweak_patch_to_placement_windows(
    patch: LayoutTweakPatch | Mapping[str, Any],
    *,
    observation: Mapping[str, Any] | None = None,
) -> tuple[PlacementWindowSpec, ...]:
    """Extract executable placement-window handles from a tweak patch."""

    patch_obj = patch if isinstance(patch, LayoutTweakPatch) else layout_tweak_patch_from_dict(patch)
    windows: list[PlacementWindowSpec] = []
    for operation in patch_obj.operations:
        op = str(operation.op).lower()
        if op == "nudge":
            window = _nudge_placement_window(operation, patch_obj.patch_id, observation=observation)
        elif op == "placement_window":
            window = _explicit_placement_window(operation, patch_obj.patch_id)
        else:
            window = None
        if window is not None:
            windows.append(window)
    return _merge_placement_windows((), tuple(windows))


def _operation_from_dict(data: Any) -> LayoutTweakOperation:
    row = _mapping(data)
    return LayoutTweakOperation(
        op=str(row.get("op", "")),
        target=str(row.get("target", "")),
        source=str(row.get("source", "")),
        target_group=str(row.get("target_group", "")),
        axis=str(row.get("axis", "")),
        edge=str(row.get("edge", "")),
        dx_tracks=_optional_int(row.get("dx_tracks")),
        dy_tracks=_optional_int(row.get("dy_tracks")),
        dx_um=_optional_float(row.get("dx_um")),
        dy_um=_optional_float(row.get("dy_um")),
        min_x_tracks=_optional_int(row.get("min_x_tracks")),
        max_x_tracks=_optional_int(row.get("max_x_tracks")),
        min_y_tracks=_optional_int(row.get("min_y_tracks")),
        max_y_tracks=_optional_int(row.get("max_y_tracks")),
        target_x_tracks=_optional_int(row.get("target_x_tracks")),
        target_y_tracks=_optional_int(row.get("target_y_tracks")),
        window_margin_tracks=_optional_int(row.get("window_margin_tracks")),
        spacing_um=_optional_float(row.get("spacing_um")),
        spacing_options_um=tuple(float(value) for value in tuple(row.get("spacing_options_um", ()) or ())),
        topology_options=_unique_strings(tuple(row.get("topology_options", ()) or ())),
        layer=str(row.get("layer", "")),
        lane=_optional_int(row.get("lane")),
        channel_side=str(row.get("channel_side", "")),
        route_name=str(row.get("route_name", "")),
        solver=str(row.get("solver", "")),
        risk=str(row.get("risk", "")),
        hard=bool(row.get("hard", False)),
        weight=int(row.get("weight", 1) or 1),
        observation_refs=_unique_strings(tuple(row.get("observation_refs", ()) or ())),
        metadata=dict(_mapping(row.get("metadata"))),
    )


def _apply_pattern_candidate(
    patterns: Sequence[DevicePatternSpec],
    operation: LayoutTweakOperation,
    patch_id: str,
) -> tuple[tuple[DevicePatternSpec, ...], bool]:
    spacing = _selected_spacing_um(operation)
    topology_options = tuple(str(value) for value in operation.topology_options if str(value))
    if (spacing is None and not topology_options) or not operation.target:
        return tuple(patterns), False
    changed = False
    updated: list[DevicePatternSpec] = []
    for pattern in patterns:
        if pattern.name != operation.target:
            updated.append(pattern)
            continue
        candidates = tuple(pattern.candidates)
        topology_changed = False
        if topology_options and candidates:
            filtered = tuple(
                candidate
                for candidate in candidates
                if _candidate_matches_topology_options(candidate, topology_options)
            )
            if filtered:
                candidates = filtered
                topology_changed = True
        spacing_changed = spacing is not None
        if spacing_changed:
            candidates = tuple(_with_candidate_spacing(candidate, float(spacing)) for candidate in candidates)
        notes = pattern.notes
        if spacing_changed:
            notes = _append_note(notes, f"Layout tweak {patch_id}: internal spacing {float(spacing):g}um.")
        if topology_changed:
            notes = _append_note(
                notes,
                f"Layout tweak {patch_id}: topology candidates {tuple(topology_options)}.",
            )
        updated.append(
            replace(
                pattern,
                spacing_um=float(spacing) if spacing is not None else pattern.spacing_um,
                candidates=candidates,
                notes=notes,
            )
        )
        changed = spacing_changed or topology_changed
    return tuple(updated), changed


def _selected_spacing_um(operation: LayoutTweakOperation) -> float | None:
    values = [float(value) for value in operation.spacing_options_um if float(value) > 0.0]
    if operation.spacing_um is not None and float(operation.spacing_um) > 0.0:
        values.append(float(operation.spacing_um))
    return min(values) if values else None


def _with_candidate_spacing(candidate: PatternCandidateSpec, spacing_um: float) -> PatternCandidateSpec:
    return replace(candidate, spacing_um=float(spacing_um))


def _candidate_matches_topology_options(
    candidate: PatternCandidateSpec,
    options: Sequence[str],
) -> bool:
    return any(_candidate_matches_topology_option(candidate, option) for option in options)


def _candidate_matches_topology_option(candidate: PatternCandidateSpec, option: str) -> bool:
    text = str(option or "").strip().lower()
    if not text:
        return False
    name = str(candidate.name or "").lower()
    rows = max(1, int(candidate.rows))
    cols = max(1, int(candidate.cols))
    if fnmatch.fnmatchcase(name, text) or text in name:
        return True
    if text in {f"{rows}x{cols}", f"{rows}r{cols}c", f"rows={rows},cols={cols}", f"r{rows}c{cols}"}:
        return True
    if text in {"wide", "horizontal", "row", "row_like"}:
        return cols > rows
    if text in {"tall", "vertical", "column", "column_like"}:
        return rows > cols
    if text in {"square", "balanced", "near_square"}:
        return abs(rows - cols) <= 1
    return False


def _compact_gap_pack(operation: LayoutTweakOperation, patch_id: str) -> PackConstraintSpec | None:
    source = str(operation.source or operation.target_group)
    target = str(operation.target)
    if not source or not target or source == target:
        return None
    axis = str(operation.axis or "both").lower()
    if axis == "x":
        width_weight, height_weight = 14, 4
    elif axis == "y":
        width_weight, height_weight = 4, 14
    else:
        width_weight, height_weight = 10, 10
    return PackConstraintSpec(
        _safe_name(f"tweak_compact_{source}_{target}_{axis}"),
        (source, target),
        max_width_um=None,
        max_height_um=None,
        weight=24,
        width_weight=width_weight,
        height_weight=height_weight,
        area_weight=2,
        notes=f"Layout tweak {patch_id}: compact local envelope on axis={axis}.",
    )


def _align_edge_objective(operation: LayoutTweakOperation, patch_id: str) -> LayoutObjectiveTermSpec | None:
    source = str(operation.source or operation.target_group)
    target = str(operation.target)
    if not source or not target or source == target:
        return None
    edge = str(operation.edge or operation.axis or "both").lower()
    axis = str(operation.axis or _axis_from_edge(edge) or "both").lower()
    return LayoutObjectiveTermSpec(
        _safe_name(f"tweak_align_{source}_{target}_{edge}"),
        "edge_alignment",
        patterns=(source, target),
        weight=8,
        axis=axis,
        target=edge,
        notes=f"Layout tweak {patch_id}: soft edge alignment edge={edge}.",
    )


def _nudge_placement_window(
    operation: LayoutTweakOperation,
    patch_id: str,
    *,
    observation: Mapping[str, Any] | None = None,
) -> PlacementWindowSpec | None:
    target = str(operation.target or operation.target_group)
    if not target:
        return None
    origin = _operation_or_observation_origin_tracks(operation, observation)
    if origin is None:
        return None
    ox, oy = origin
    dx = int(operation.dx_tracks or 0)
    dy = int(operation.dy_tracks or 0)
    pitch = _track_pitch_from_observation(observation)
    if operation.dx_um is not None and pitch is not None:
        dx += int(round(float(operation.dx_um) / max(pitch, 1e-9)))
    if operation.dy_um is not None and pitch is not None:
        dy += int(round(float(operation.dy_um) / max(pitch, 1e-9)))
    axis = str(operation.axis or _axis_from_delta(dx, dy)).lower()
    target_x = ox + dx if axis in {"x", "both", "xy"} else None
    target_y = oy + dy if axis in {"y", "both", "xy"} else None
    margin = operation.window_margin_tracks
    min_x = max(0, target_x - int(margin)) if target_x is not None and margin is not None else None
    max_x = max(0, target_x + int(margin)) if target_x is not None and margin is not None else None
    min_y = max(0, target_y - int(margin)) if target_y is not None and margin is not None else None
    max_y = max(0, target_y + int(margin)) if target_y is not None and margin is not None else None
    return PlacementWindowSpec(
        _safe_name(f"tweak_nudge_{target}_{axis}"),
        target,
        min_x_tracks=min_x,
        max_x_tracks=max_x,
        min_y_tracks=min_y,
        max_y_tracks=max_y,
        target_x_tracks=target_x,
        target_y_tracks=target_y,
        weight=max(1, int(operation.weight)),
        hard=bool(operation.hard),
        notes=f"Layout tweak {patch_id}: nudge target by dx={dx}, dy={dy} tracks on axis={axis}.",
    )


def _explicit_placement_window(operation: LayoutTweakOperation, patch_id: str) -> PlacementWindowSpec | None:
    target = str(operation.target or operation.target_group)
    if not target:
        return None
    return PlacementWindowSpec(
        _safe_name(f"tweak_window_{target}"),
        target,
        min_x_tracks=operation.min_x_tracks,
        max_x_tracks=operation.max_x_tracks,
        min_y_tracks=operation.min_y_tracks,
        max_y_tracks=operation.max_y_tracks,
        target_x_tracks=operation.target_x_tracks,
        target_y_tracks=operation.target_y_tracks,
        weight=max(1, int(operation.weight)),
        hard=bool(operation.hard),
        notes=f"Layout tweak {patch_id}: explicit placement window.",
    )


def _route_lane_resource(
    existing: Sequence[RouteResourceSpec],
    operation: LayoutTweakOperation,
    patch_id: str,
) -> RouteResourceSpec | None:
    name = str(operation.route_name or operation.target or operation.target_group)
    if not name:
        return None
    metadata = _mapping(operation.metadata)
    match = str(metadata.get("match", "net") or "net").lower()
    if match not in {"net", "prefix"}:
        match = "net"
    current = _find_route_resource(existing, name, match)
    layer = str(operation.layer or "")
    allowed_layers = (layer,) if layer else ()
    route_policy = dict(_mapping(getattr(current, "route_policy", {}))) if current is not None else {}
    route_policy.update(dict(_mapping(metadata.get("route_policy"))))
    return RouteResourceSpec(
        name=name,
        match=match,
        layer=layer or (current.layer if current is not None else ""),
        allowed_layers=allowed_layers or (tuple(current.allowed_layers) if current is not None else ()),
        forbidden_layers=tuple(current.forbidden_layers) if current is not None else (),
        cyclic_layers=tuple(current.cyclic_layers) if current is not None else (),
        lane=operation.lane if operation.lane is not None else (current.lane if current is not None else None),
        cyclic_lanes=tuple(current.cyclic_lanes) if current is not None else (),
        avoid_nets=tuple(current.avoid_nets) if current is not None else (),
        avoid_prefixes=tuple(current.avoid_prefixes) if current is not None else (),
        style=str(metadata.get("style", "")) or (current.style if current is not None else ""),
        channel_orientation=str(metadata.get("channel_orientation", ""))
        or (current.channel_orientation if current is not None else ""),
        channel_side=str(operation.channel_side or metadata.get("channel_side", ""))
        or (current.channel_side if current is not None else ""),
        channel_offset_um=_optional_float(metadata.get("channel_offset_um"))
        if metadata.get("channel_offset_um") is not None
        else (current.channel_offset_um if current is not None else None),
        dogleg_side=str(metadata.get("dogleg_side", "")) or (current.dogleg_side if current is not None else ""),
        dogleg_offset_um=_optional_float(metadata.get("dogleg_offset_um"))
        if metadata.get("dogleg_offset_um") is not None
        else (current.dogleg_offset_um if current is not None else None),
        dogleg_offset_step_um=current.dogleg_offset_step_um if current is not None else None,
        terminal_escape_style=str(metadata.get("terminal_escape_style", ""))
        or (current.terminal_escape_style if current is not None else ""),
        terminal_escape_um=_optional_float(metadata.get("terminal_escape_um"))
        if metadata.get("terminal_escape_um") is not None
        else (current.terminal_escape_um if current is not None else None),
        route_policy=route_policy,
        notes=_append_note(
            current.notes if current is not None else "",
            f"Layout tweak {patch_id}: route resource override layer={layer or '-'} lane={operation.lane}.",
        ),
    )


def _find_route_resource(
    resources: Sequence[RouteResourceSpec],
    name: str,
    match: str,
) -> RouteResourceSpec | None:
    for resource in reversed(tuple(resources or ())):
        if str(resource.name) == str(name) and str(resource.match or "net").lower() == str(match or "net").lower():
            return resource
    return None


def _operation_or_observation_origin_tracks(
    operation: LayoutTweakOperation,
    observation: Mapping[str, Any] | None,
) -> tuple[int, int] | None:
    if operation.target_x_tracks is not None or operation.target_y_tracks is not None:
        return (int(operation.target_x_tracks or 0), int(operation.target_y_tracks or 0))
    metadata = _mapping(operation.metadata)
    bbox = _sequence4(metadata.get("baseline_bbox_tracks") or metadata.get("bbox_tracks"))
    if bbox is not None:
        return int(round(bbox[0])), int(round(bbox[1]))
    if observation is None:
        return None
    groups = _mapping(_mapping(observation.get("entities")).get("groups"))
    row = _mapping(groups.get(operation.target) or groups.get(operation.target_group))
    bbox = _sequence4(row.get("bbox_tracks"))
    if bbox is not None:
        return int(round(bbox[0])), int(round(bbox[1]))
    tweak_obs = _layout_tweakability_data(observation)
    tweak_groups = _mapping(tweak_obs.get("groups"))
    row = _mapping(tweak_groups.get(operation.target) or tweak_groups.get(operation.target_group))
    bbox = _sequence4(row.get("bbox_tracks"))
    if bbox is not None:
        return int(round(bbox[0])), int(round(bbox[1]))
    return None


def _layout_tweakability_data(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    for row_obj in tuple(observation.get("observations", ()) or ()):
        row = _mapping(row_obj)
        if row.get("kind") == "layout_tweakability_facts":
            return _mapping(row.get("data"))
    return {}


def _track_pitch_from_observation(observation: Mapping[str, Any] | None) -> float | None:
    if observation is None:
        return None
    try:
        return float(_mapping(observation.get("unit")).get("track_pitch_um"))
    except (TypeError, ValueError):
        return None


def _merge_pack_constraints(
    existing: Sequence[PackConstraintSpec],
    additions: Sequence[PackConstraintSpec],
) -> tuple[PackConstraintSpec, ...]:
    result = list(tuple(existing or ()))
    index_by_name = {str(pack.name): idx for idx, pack in enumerate(result)}
    for addition in additions:
        if addition.name not in index_by_name:
            index_by_name[addition.name] = len(result)
            result.append(addition)
            continue
        current = result[index_by_name[addition.name]]
        result[index_by_name[addition.name]] = replace(
            current,
            patterns=tuple(dict.fromkeys(tuple(current.patterns) + tuple(addition.patterns))),
            max_width_um=_min_optional(current.max_width_um, addition.max_width_um),
            max_height_um=_min_optional(current.max_height_um, addition.max_height_um),
            weight=max(int(current.weight), int(addition.weight)),
            width_weight=max(int(current.width_weight), int(addition.width_weight)),
            height_weight=max(int(current.height_weight), int(addition.height_weight)),
            area_weight=max(int(current.area_weight), int(addition.area_weight)),
            notes=_append_note(current.notes, addition.notes),
        )
    return tuple(result)


def _merge_placement_windows(
    existing: Sequence[PlacementWindowSpec],
    additions: Sequence[PlacementWindowSpec],
) -> tuple[PlacementWindowSpec, ...]:
    result = list(tuple(existing or ()))
    index_by_name = {str(window.name): idx for idx, window in enumerate(result)}
    for addition in additions:
        if addition.name not in index_by_name:
            index_by_name[addition.name] = len(result)
            result.append(addition)
            continue
        current = result[index_by_name[addition.name]]
        result[index_by_name[addition.name]] = replace(
            current,
            min_x_tracks=_max_optional_int(current.min_x_tracks, addition.min_x_tracks),
            max_x_tracks=_min_optional_int(current.max_x_tracks, addition.max_x_tracks),
            min_y_tracks=_max_optional_int(current.min_y_tracks, addition.min_y_tracks),
            max_y_tracks=_min_optional_int(current.max_y_tracks, addition.max_y_tracks),
            target_x_tracks=addition.target_x_tracks if addition.target_x_tracks is not None else current.target_x_tracks,
            target_y_tracks=addition.target_y_tracks if addition.target_y_tracks is not None else current.target_y_tracks,
            weight=max(int(current.weight), int(addition.weight)),
            hard=bool(current.hard or addition.hard),
            notes=_append_note(current.notes, addition.notes),
        )
    return tuple(result)


def _merge_objective_terms(
    existing: Sequence[LayoutObjectiveTermSpec],
    additions: Sequence[LayoutObjectiveTermSpec],
) -> tuple[LayoutObjectiveTermSpec, ...]:
    result = list(tuple(existing or ()))
    index_by_name = {str(term.name): idx for idx, term in enumerate(result)}
    for addition in additions:
        if addition.name not in index_by_name:
            index_by_name[addition.name] = len(result)
            result.append(addition)
            continue
        current = result[index_by_name[addition.name]]
        result[index_by_name[addition.name]] = replace(
            current,
            patterns=tuple(dict.fromkeys(tuple(current.patterns) + tuple(addition.patterns))),
            devices=tuple(dict.fromkeys(tuple(current.devices) + tuple(addition.devices))),
            weight=max(int(current.weight), int(addition.weight)),
            axis=addition.axis or current.axis,
            metric=addition.metric or current.metric,
            target=addition.target or current.target,
            notes=_append_note(current.notes, addition.notes),
        )
    return tuple(result)


def _merge_route_resources(
    existing: Sequence[RouteResourceSpec],
    additions: Sequence[RouteResourceSpec],
) -> tuple[RouteResourceSpec, ...]:
    result = list(tuple(existing or ()))
    index_by_key = {
        (str(resource.name), str(resource.match or "net").lower()): idx
        for idx, resource in enumerate(result)
    }
    for addition in additions:
        key = (str(addition.name), str(addition.match or "net").lower())
        if key not in index_by_key:
            index_by_key[key] = len(result)
            result.append(addition)
        else:
            result[index_by_key[key]] = addition
    return tuple(result)


def _axis_from_edge(edge: str) -> str:
    normalized = str(edge or "").lower()
    if normalized in {"left", "right", "x", "xmin", "xmax"}:
        return "x"
    if normalized in {"top", "bottom", "y", "ymin", "ymax"}:
        return "y"
    return "both"


def _axis_from_delta(dx_tracks: int | None, dy_tracks: int | None) -> str:
    dx = int(dx_tracks or 0)
    dy = int(dy_tracks or 0)
    if dx and not dy:
        return "x"
    if dy and not dx:
        return "y"
    return "both"


def _append_note(left: str, right: str) -> str:
    parts = [str(left).strip(), str(right).strip()]
    return " ".join(part for part in parts if part)


def _metadata_value_present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return bool(tuple(value))
    return True


def _safe_name(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_]+", "_", str(value))
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "layout_tweak"


def _min_optional(left: float | None, right: float | None) -> float | None:
    values = [float(value) for value in (left, right) if value is not None]
    return min(values) if values else None


def _min_optional_int(left: int | None, right: int | None) -> int | None:
    values = [int(value) for value in (left, right) if value is not None]
    return min(values) if values else None


def _max_optional_int(left: int | None, right: int | None) -> int | None:
    values = [int(value) for value in (left, right) if value is not None]
    return max(values) if values else None


def _unique_strings(values: Sequence[object]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sequence4(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) != 4:
        return None
    try:
        return tuple(float(item) for item in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return None


def _clean_json(value: Any) -> Any:
    if is_dataclass(value):
        return _clean_json(asdict(value))
    if isinstance(value, Mapping):
        result = {}
        for key, row in value.items():
            if _drop_from_json(row):
                continue
            result[str(key)] = _clean_json(row)
        return result
    if isinstance(value, (list, tuple)):
        return [_clean_json(row) for row in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _drop_from_json(value: Any) -> bool:
    return value is None or value == "" or value == () or value == [] or value == {}
