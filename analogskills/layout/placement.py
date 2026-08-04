"""Analog placement generators, analyzers, and local tuning operators."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from analogskills.contracts import AnalogFloorplanContract, AnalogPlacementGroup, AnalogPlacementObjective, AnalogPlacementStrategy, DeviceRole, LayoutConstraintSet, NetRole, TopologyGraph
from analogskills.layout.constraints import extract_layout_constraints


@dataclass(frozen=True)
class Placement:
    name: str
    x_um: float
    y_um: float
    orient: str = "R0"
    role: str = ""


@dataclass(frozen=True)
class PlacementCandidate:
    placements: tuple[Placement, ...]
    score: float
    costs: dict[str, float]
    issues: tuple[str, ...] = ()


def generate_placement(
    graph: TopologyGraph,
    constraints: LayoutConstraintSet | None = None,
    pdk: object | None = None,
    *,
    pitch_um: float | None = None,
    row_pitch_um: float = 2.0,
    floorplan_contract: AnalogFloorplanContract | None = None,
) -> tuple[Placement, ...]:
    """Generate a constraint-aware seed placement for a topology graph.

    The generator is deliberately conservative: it emits device-level seed
    coordinates suitable for PCell planning and uses expanded unit placements
    only for common-centroid/interdigitated groups where the existing placement
    analyzers already understand role centroids and dummies.
    """
    base_constraints = constraints or graph.layout_constraints
    active_constraints = extract_layout_constraints(graph, base_constraints=base_constraints)
    active_contract = floorplan_contract or _build_floorplan_contract(graph, active_constraints)
    device_role_map = {device.name: device.role for device in graph.devices.values()}
    site = getattr(pdk, "placement_site", None)
    base_pitch = float(site.device_pitch_um) if site is not None else _default_pitch_um(graph)
    pitch = float(pitch_um) if pitch_um is not None else base_pitch
    if pitch <= 0:
        pitch = _default_pitch_um(graph)
    row_pitch = float(row_pitch_um)
    if site is not None and row_pitch_um == 2.0:
        row_pitch = float(site.row_pitch_um)
    if _is_two_stage_miller_ota(graph):
        return _two_stage_miller_seed_placement(graph, pitch, row_pitch)
    if _is_three_stage_miller_ota(graph):
        return _three_stage_miller_seed_placement(graph, pitch, row_pitch)
    if _is_folded_cascode_ota(graph):
        return _folded_cascode_ota_seed_placement(graph, pitch, row_pitch)
    if _is_telescopic_ota(graph):
        return _telescopic_ota_seed_placement(graph, pitch, row_pitch)
    if _is_bandgap_reference(graph):
        return _bandgap_seed_placement(graph, pitch, row_pitch)
    if _is_pmos_pass_ldo(graph):
        return _pmos_pass_ldo_seed_placement(graph, pitch, row_pitch)
    if _is_reference_buffer(graph):
        return _reference_buffer_seed_placement(graph, pitch, row_pitch)
    if _is_mdac_stage(graph):
        return _mdac_stage_seed_placement(graph, pitch, row_pitch)
    if _is_pipeline_adc_frontend(graph):
        return _pipeline_adc_frontend_seed_placement(graph, pitch, row_pitch)
    if _is_strongarm_comparator(graph):
        placements = list(_strongarm_seed_placement(graph, pitch, row_pitch))
        placements = list(_apply_role_orient_policy(tuple(placements), pdk, device_role_map=device_role_map))
        return _finalize_seed_placements(tuple(placements), active_constraints, pdk, device_role_map=device_role_map)
    seed = _build_generic_signal_flow_seed(
        graph,
        pdk,
        pitch=pitch,
        row_pitch=row_pitch,
        floorplan_contract=active_contract,
    )
    if seed and (
        floorplan_contract is not None
        or _should_use_floorplan_seed(active_contract, explicit_contract=False)
        or _has_meaningful_layout_constraints(base_constraints)
        or len(graph.devices) >= 3
    ):
        placements = list(_apply_role_orient_policy(seed, pdk, device_role_map=device_role_map))
        return _finalize_seed_placements(tuple(placements), active_constraints, pdk, device_role_map=device_role_map)
    placed_devices: set[str] = set()
    placements: list[Placement] = []
    y_cursor = 0.0

    if not _has_meaningful_layout_constraints(base_constraints):
        seed = _build_generic_signal_flow_seed(graph, pdk, pitch=pitch, row_pitch=row_pitch, floorplan_contract=active_contract)
        placements.extend(seed)
        placed_devices.update(placement.name for placement in seed if placement.name in graph.devices)
        if placements:
            y_cursor = max((placement.y_um for placement in placements if placement.role != "dummy"), default=0.0) + row_pitch

    device_order = tuple(graph.devices)
    for group in active_constraints.matched_groups:
        devices = tuple(device for device in group.devices if device in graph.devices)
        if not devices or any(device in placed_devices for device in devices):
            continue
        if len(devices) == 2 and group.style == "common_centroid":
            cc_pitch = float(getattr(site, "common_centroid_pitch_um", pitch)) if site is not None else pitch
            placements.extend(_with_y_offset(common_centroid_pair(devices[0], devices[1], pitch_um=cc_pitch), y_cursor))
        elif group.style == "interdigitated":
            id_pitch = float(getattr(site, "interdigitated_pitch_um", pitch)) if site is not None else pitch
            placements.extend(_with_y_offset(interdigitated_current_mirror(devices[0], devices[1:], pitch_um=id_pitch, include_dummies=group.require_dummies), y_cursor))
        else:
            placements.extend(_mirror_group_placement(group.name, devices, pitch, y_cursor, include_dummies=group.require_dummies))
        placed_devices.update(devices)
        y_cursor += row_pitch

    remaining = [device for device in device_order if device not in placed_devices]
    if remaining:
        center = (len(remaining) - 1) / 2
        for idx, device in enumerate(remaining):
            x_um = (idx - center) * pitch
            if site is not None and getattr(site, "row_policy", "single") == "staggered" and int(round(y_cursor / max(row_pitch, 1e-12))) % 2 == 1:
                x_um += 0.5 * pitch
            placements.append(Placement(device, x_um, y_cursor, role=device))

    placements = list(_apply_role_orient_policy(tuple(placements), pdk, device_role_map=device_role_map))
    return _finalize_seed_placements(tuple(placements), active_constraints, pdk, device_role_map=device_role_map)


def _finalize_seed_placements(
    placements: tuple[Placement, ...],
    constraints: LayoutConstraintSet,
    pdk: object | None,
    *,
    device_role_map: Mapping[str, DeviceRole],
) -> tuple[Placement, ...]:
    site = getattr(pdk, "placement_site", None)
    symmetry_axis = getattr(site, "symmetry_axis", "y") if site is not None else "y"
    finalized = list(_apply_symmetry_groups(placements, constraints.symmetry_groups, symmetry_axis=symmetry_axis))
    finalized = list(_apply_role_row_policy(tuple(finalized), pdk, device_role_map=device_role_map))
    return tune_placement(tuple(finalized), constraints)


def resolve_device_centroids(
    placements: Sequence[Placement],
) -> dict[str, Placement]:
    """Collapse unit-level placements into one device-level centroid map.

    The placement engine may emit unit placements such as ``MIN_P_u0`` /
    ``MIN_P_u1`` while downstream flows operate on device names like
    ``MIN_P``. This helper computes one representative placement per device,
    using the arithmetic centroid of all matching unit placements.

    Explicit device-level placements override synthesized centroids.
    """

    groups: dict[str, list[Placement]] = {}
    explicit: dict[str, Placement] = {}
    for placement in placements:
        device_name = _placement_device_name(placement)
        if not device_name:
            continue
        groups.setdefault(device_name, []).append(placement)
        if placement.name == device_name:
            explicit[device_name] = placement

    resolved: dict[str, Placement] = {}
    for device_name, members in groups.items():
        direct = explicit.get(device_name)
        if direct is not None:
            resolved[device_name] = direct
            continue
        x_um = sum(member.x_um for member in members) / len(members)
        y_um = sum(member.y_um for member in members) / len(members)
        orient = _dominant_orient(members)
        role = next((member.role for member in members if member.role and member.role != "dummy"), device_name)
        resolved[device_name] = Placement(device_name, x_um, y_um, orient=orient, role=role)
    return resolved


def build_analog_placement_strategy(
    graph: TopologyGraph,
    *,
    constraints: LayoutConstraintSet | None = None,
    floorplan_contract: AnalogFloorplanContract | None = None,
) -> AnalogPlacementStrategy:
    active_constraints = extract_layout_constraints(graph, base_constraints=constraints or graph.layout_constraints)
    contract = floorplan_contract or _build_floorplan_contract(graph, active_constraints)
    row_by_role = {str(role): str(row) for role, row in contract.row_roles if str(role) and str(row)}
    groups = tuple(
        AnalogPlacementGroup(
            name=partition.name,
            devices=partition.devices,
            role=partition.role,
            anchor=partition.anchor,
            focus=partition.focus,
            order_index=partition.order_index,
            target_row=row_by_role.get(partition.role, ""),
            target_partition=partition.name,
            critical_nets=tuple(net for net in partition.nets if net in set(contract.critical_nets)),
            notes="auto-synthesized placement partition",
        )
        for partition in sorted(
            contract.partitions,
            key=lambda item: (
                item.order_index if item.order_index is not None else 10_000,
                not item.anchor,
                not item.focus,
                item.name,
            ),
        )
    )
    objectives = (
        AnalogPlacementObjective("match", weight=4.0, priority=0, notes="preserve matched device structure"),
        AnalogPlacementObjective("symmetry", weight=4.0, priority=1, notes="preserve symmetry groups"),
        AnalogPlacementObjective("critical_nets", weight=3.5, priority=2, notes="keep critical nets compact"),
        AnalogPlacementObjective("wirelength", weight=2.5, priority=3, notes="minimize signal-flow wirelength"),
        AnalogPlacementObjective("area", weight=1.5, priority=4, notes="contain placement envelope"),
    )
    notes = (
        f"skeleton={contract.intent.preferred_skeleton}",
        f"group_count={len(groups)}",
        "analytical placer should generate the initial solution before any agent ECO tuning",
    )
    return AnalogPlacementStrategy(
        groups=groups,
        objectives=objectives,
        initial_mode="analytical_seed",
        tune_with_agent=True,
        notes=notes,
    )


def common_centroid_pair(dev_a: str, dev_b: str, *, pitch_um: float = 1.0, y_um: float = 0.0) -> tuple[Placement, ...]:
    # Four unit cells in ABBA order with edge dummies.
    return (
        Placement(f"DUMMY_{dev_a}_L", -2.5 * pitch_um, y_um, role="dummy"),
        Placement(f"{dev_a}_u0", -1.5 * pitch_um, y_um, role=dev_a),
        Placement(f"{dev_b}_u0", -0.5 * pitch_um, y_um, role=dev_b),
        Placement(f"{dev_b}_u1", 0.5 * pitch_um, y_um, role=dev_b),
        Placement(f"{dev_a}_u1", 1.5 * pitch_um, y_um, role=dev_a),
        Placement(f"DUMMY_{dev_b}_R", 2.5 * pitch_um, y_um, role="dummy"),
    )


def interdigitated_current_mirror(
    reference: str,
    outputs: tuple[str, ...],
    *,
    ratios: tuple[int, ...] | None = None,
    pitch_um: float = 1.0,
    y_um: float = 0.0,
    include_dummies: bool = True,
) -> tuple[Placement, ...]:
    if not outputs:
        raise ValueError("at least one mirror output is required")
    ratios = ratios or tuple(1 for _ in outputs)
    if len(ratios) != len(outputs):
        raise ValueError("ratios must match outputs")
    if any(ratio < 1 for ratio in ratios):
        raise ValueError("mirror ratios must be positive integers")

    roles: list[str] = []
    max_ratio = max(1, *ratios)
    for unit in range(max_ratio):
        roles.append(reference)
        for output, ratio in zip(outputs, ratios):
            if unit < ratio:
                roles.append(output)
    sequence = roles + list(reversed(roles))
    if include_dummies:
        sequence = ["dummy", *sequence, "dummy"]

    counts: dict[str, int] = {}
    placements: list[Placement] = []
    center = (len(sequence) - 1) / 2
    for idx, role in enumerate(sequence):
        x_um = (idx - center) * pitch_um
        if role == "dummy":
            suffix = "L" if idx == 0 else "R" if idx == len(sequence) - 1 else str(counts.get(role, 0))
            placements.append(Placement(f"DUMMY_{suffix}", x_um, y_um, role="dummy"))
            counts[role] = counts.get(role, 0) + 1
            continue
        unit_idx = counts.get(role, 0)
        orient = "R0" if idx <= center else "MY"
        placements.append(Placement(f"{role}_u{unit_idx}", x_um, y_um, orient=orient, role=role))
        counts[role] = unit_idx + 1
    return tuple(placements)


def analyze_placement(
    placements: tuple[Placement, ...],
    constraints: LayoutConstraintSet,
    pdk: object | None = None,
    *,
    graph: TopologyGraph | None = None,
) -> dict[str, object]:
    issues: list[str] = []
    device_role_map = _device_role_map(graph)
    profile = _analog_placement_profile(pdk)
    match_tol = max(float(profile.get("match_tolerance_um", 1e-6) or 0.0), 0.0)
    symmetry_tol = max(float(profile.get("symmetry_tolerance_um", 1e-6) or 0.0), 0.0)
    row_tol = max(float(profile.get("row_alignment_tolerance_um", 1e-6) or 0.0), 0.0)
    by_role: dict[str, list[Placement]] = {}
    for p in placements:
        by_role.setdefault(p.role or p.name, []).append(p)
    for group in constraints.matched_groups:
        ys = []
        xs = []
        role_centroids = []
        for dev in group.devices:
            units = by_role.get(dev) or [p for p in placements if p.name == dev]
            ys.extend(p.y_um for p in units)
            xs.extend(p.x_um for p in units)
            if units:
                role_centroids.append(sum(p.x_um for p in units) / len(units))
        if ys and max(ys) - min(ys) > match_tol:
            issues.append(f"matched group {group.name} y-mismatch")
        if group.require_dummies and not any(p.role == "dummy" and "DUMMY" in p.name for p in placements):
            issues.append(f"matched group {group.name} missing dummies")
        if group.style in {"common_centroid", "interdigitated"} and xs:
            centroid = sum(xs) / len(xs)
            local_axis = 0.5 * (min(xs) + max(xs))
            centroid_offset = centroid - local_axis
            if abs(centroid_offset) > match_tol:
                issues.append(f"matched group {group.name} centroid offset {centroid_offset:.4g}um")
            if role_centroids and max(role_centroids) - min(role_centroids) > match_tol:
                issues.append(f"matched group {group.name} role centroid mismatch")
    for issue in _placement_role_row_policy_issues(placements, pdk, tolerance_um=row_tol, device_role_map=device_role_map):
        if issue not in issues:
            issues.append(issue)
    return {
        "passed": not issues,
        "issues": issues,
        "count": len(placements),
        "analog_profile": {
            "match_tolerance_um": match_tol,
            "symmetry_tolerance_um": symmetry_tol,
            "row_alignment_tolerance_um": row_tol,
        },
    }


def rank_placement_candidates(
    candidates: tuple[tuple[Placement, ...], ...] | list[tuple[Placement, ...]],
    constraints: LayoutConstraintSet | None = None,
    *,
    graph: TopologyGraph | None = None,
    pdk: object | None = None,
    target_aspect: float = 1.0,
    placement_seed_metadata: dict[str, object] | None = None,
    weights: dict[str, float] | None = None,
    top_k: int | None = None,
) -> tuple[PlacementCandidate, ...]:
    """Rank placement alternatives with transparent layout costs."""

    constraints = constraints or LayoutConstraintSet()
    weight_map = {
        "area": 0.05,
        "aspect": 0.1,
        "hpwl": 0.1,
        "routing_overflow": 2.0,
        "routing_peak_utilization": 0.25,
        "y_spread": 0.25,
        "centroid": 0.2,
        "matched_group_violations": 2.0,
        "matched_group_dummy_violations": 1.0,
        "matched_group_centroid_violations": 1.5,
        "symmetry_group_violations": 2.0,
        "row_policy_violations": 1.0,
        "focus_partition_target_shortfall": 0.2,
        "anchor_partition_target_overflow": 0.2,
        "partition_order_violations": 1.0,
        "anchor_partition_spread": 0.2,
        "focus_partition_separation": 0.2,
        "pcell_partition_internal_spread": 0.2,
        "pex_focus_partition_spread": 0.2,
        "reference_sensitive_partition_spread": 0.15,
        "feedback_sensitive_partition_spread": 0.2,
        "issues": 1.0,
    }
    if weights:
        weight_map.update(weights)
    rows = []
    for placement in candidates:
        normalized = tuple(placement)
        costs = _placement_costs(normalized, constraints, target_aspect, graph, pdk, placement_seed_metadata=placement_seed_metadata)
        raw_score = sum(weight_map.get(name, 0.0) * value for name, value in costs.items())
        report = analyze_placement(normalized, constraints, pdk=pdk, graph=graph)
        rows.append(PlacementCandidate(normalized, raw_score, costs, tuple(str(issue) for issue in report["issues"])))
    ranked = tuple(sorted(rows, key=lambda row: (row.score, len(row.issues), _placement_bbox_area(row.placements))))
    return ranked if top_k is None else ranked[:top_k]


def tune_placement(placements: tuple[Placement, ...], constraints: LayoutConstraintSet) -> tuple[Placement, ...]:
    tuned = list(placements)
    for group in constraints.matched_groups:
        indices = [idx for idx, placement in enumerate(tuned) if placement.role in group.devices or placement.name in group.devices]
        if group.style == "common_centroid" and len(group.devices) == 2:
            existing_roles = {tuned[idx].role or tuned[idx].name for idx in indices}
            if set(group.devices).issubset(existing_roles):
                y = sum(tuned[idx].y_um for idx in indices) / len(indices) if indices else 0.0
                pitch = _pitch_um(tuple(tuned))
                replacement = common_centroid_pair(group.devices[0], group.devices[1], pitch_um=pitch, y_um=y)
                preserved_anchors = [
                    p
                    for idx, p in enumerate(tuned)
                    if idx in indices and p.name in group.devices and p.role == "anchor"
                ]
                tuned = [p for idx, p in enumerate(tuned) if idx not in indices and p.role != "dummy"]
                tuned.extend(preserved_anchors)
                tuned.extend(replacement)
                continue
        if indices:
            target_y = sum(tuned[idx].y_um for idx in indices) / len(indices)
            for idx in indices:
                p = tuned[idx]
                tuned[idx] = Placement(p.name, p.x_um, target_y, p.orient, p.role)
        if group.require_dummies and not any(p.role == "dummy" for p in tuned):
            pitch = _pitch_um(tuple(tuned))
            min_x = min((p.x_um for p in tuned), default=0.0)
            max_x = max((p.x_um for p in tuned), default=0.0)
            y = tuned[indices[0]].y_um if indices else 0.0
            tuned.insert(0, Placement(f"DUMMY_{group.name}_L", min_x - pitch, y, role="dummy"))
            tuned.append(Placement(f"DUMMY_{group.name}_R", max_x + pitch, y, role="dummy"))
    return tuple(tuned)


def _pitch_um(placements: tuple[Placement, ...]) -> float:
    xs = sorted({p.x_um for p in placements})
    deltas = [b - a for a, b in zip(xs, xs[1:]) if b - a > 1e-9]
    return min(deltas) if deltas else 1.0


def _has_meaningful_layout_constraints(constraints: LayoutConstraintSet) -> bool:
    return bool(
        constraints.matched_groups
        or constraints.symmetry_groups
        or constraints.routing
        or constraints.critical_nets
        or constraints.standard_cell is not None
    )


def _build_floorplan_contract(
    graph: TopologyGraph,
    constraints: LayoutConstraintSet,
) -> AnalogFloorplanContract:
    from analogskills.layout.floorplan import build_analog_floorplan_contract

    return build_analog_floorplan_contract(graph, constraints=constraints)


def _build_generic_signal_flow_seed(
    graph: TopologyGraph,
    pdk: object | None,
    *,
    pitch: float,
    row_pitch: float,
    floorplan_contract: AnalogFloorplanContract | None = None,
) -> tuple[Placement, ...]:
    if len(graph.devices) < 2 or not graph.nets:
        return ()
    from analogskills.layout.floorplan import build_global_placement_seed, rank_global_placement_seeds

    site = getattr(pdk, "placement_site", None)
    grid_um = float(getattr(getattr(pdk, "rules", None), "grid_step_um", 0.05) or 0.05)
    min_channel = max(grid_um, 0.75 * pitch, 0.35 * row_pitch)
    preferred_channel = max(grid_um, 1.0 * pitch, 0.5 * row_pitch)
    relaxed_channel = max(preferred_channel, 1.5 * pitch, 0.75 * row_pitch)
    channels = tuple(
        dict.fromkeys(
            round(value, 6)
            for value in (
                min_channel,
                preferred_channel,
                relaxed_channel,
                float(getattr(site, "device_pitch_um", pitch) or pitch),
            )
            if value > 0.0
        )
    )
    seeds = tuple(
        build_global_placement_seed(
            graph,
            pdk=pdk,
            row_height_um=row_pitch,
            channel_um=channel_um,
            floorplan_contract=floorplan_contract,
        )
        for channel_um in channels
    )
    if not seeds:
        return ()
    ranked = rank_global_placement_seeds(seeds)
    return ranked[0].seed.placements if ranked else seeds[0].placements


def _should_use_floorplan_seed(
    contract: AnalogFloorplanContract,
    *,
    explicit_contract: bool,
) -> bool:
    if explicit_contract:
        return True
    return contract.intent.preferred_skeleton in {
        "comparator_latch",
        "two_stage_ota",
        "three_stage_ota",
        "pipeline_adc_frontend",
        "differential_row",
    }


def _placement_costs(
    placements: tuple[Placement, ...],
    constraints: LayoutConstraintSet,
    target_aspect: float,
    graph: TopologyGraph | None,
    pdk: object | None,
    *,
    placement_seed_metadata: dict[str, object] | None = None,
) -> dict[str, float]:
    area = _placement_bbox_area(placements)
    width, height = _placement_bbox_dimensions(placements)
    aspect = width / height
    report = analyze_placement(placements, constraints, pdk=pdk, graph=graph)
    constraint_costs = _placement_constraint_costs(placements, constraints, pdk=pdk, graph=graph)
    hierarchy_costs = _placement_hierarchy_costs(placements, graph, pdk=pdk, placement_seed_metadata=placement_seed_metadata)
    routing_costs = _placement_routability_costs(placements, graph)
    return {
        "area": area,
        "aspect": abs(aspect - target_aspect) / max(target_aspect, 1e-12),
        "hpwl": _placement_hpwl(placements, constraints, graph),
        **routing_costs,
        "y_spread": _matched_group_y_spread(placements, constraints),
        "centroid": _matched_group_centroid_error(placements, constraints),
        **constraint_costs,
        **hierarchy_costs,
        "issues": float(len(report["issues"])),
    }


def _placement_routability_costs(
    placements: tuple[Placement, ...],
    graph: TopologyGraph | None,
) -> dict[str, float]:
    if graph is None:
        return {"routing_overflow": 0.0, "routing_peak_utilization": 0.0}
    from .routability import analyze_placement_routability

    report = analyze_placement_routability(placements, graph)
    return {
        "routing_overflow": report.total_overflow,
        "routing_peak_utilization": report.peak_utilization,
    }


def _placement_bbox(placements: tuple[Placement, ...]) -> tuple[float, float, float, float]:
    if not placements:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(p.x_um for p in placements),
        min(p.y_um for p in placements),
        max(p.x_um for p in placements),
        max(p.y_um for p in placements),
    )


def _placement_bbox_area(placements: tuple[Placement, ...]) -> float:
    width, height = _placement_bbox_dimensions(placements)
    return width * height


def _placement_bbox_dimensions(placements: tuple[Placement, ...]) -> tuple[float, float]:
    x0, y0, x1, y1 = _placement_bbox(placements)
    pitch = _pitch_um(placements)
    width = max(x1 - x0 + pitch, pitch)
    height = max(y1 - y0 + pitch, pitch)
    return width, height


def _placement_hpwl(placements: tuple[Placement, ...], constraints: LayoutConstraintSet, graph: TopologyGraph | None) -> float:
    if graph is not None:
        hpwl = _placement_hpwl_from_graph(placements, graph)
        if hpwl > 0:
            return hpwl
    return _placement_hpwl_proxy(placements, constraints)


def _placement_hpwl_from_graph(placements: tuple[Placement, ...], graph: TopologyGraph) -> float:
    centers = _placement_centers_by_device(placements)
    hpwl = 0.0
    for net in graph.nets.values():
        points = []
        for terminal in net.terminals:
            if terminal.device in centers:
                points.append(centers[terminal.device])
        if len(points) < 2:
            continue
        hpwl += (max(x for x, _y in points) - min(x for x, _y in points)) + (max(y for _x, y in points) - min(y for _x, y in points))
    return hpwl


def _placement_centers_by_device(placements: tuple[Placement, ...]) -> dict[str, tuple[float, float]]:
    by_role = _placements_by_role(placements)
    centers: dict[str, tuple[float, float]] = {}
    for role, units in by_role.items():
        if role == "dummy":
            continue
        centers[role] = (
            sum(unit.x_um for unit in units) / len(units),
            sum(unit.y_um for unit in units) / len(units),
        )
    return centers


def _placement_hpwl_proxy(placements: tuple[Placement, ...], constraints: LayoutConstraintSet) -> float:
    by_role = _placements_by_role(placements)
    hpwl = 0.0
    for group in constraints.matched_groups:
        units = [unit for dev in group.devices for unit in by_role.get(dev, ())]
        if len(units) < 2:
            continue
        hpwl += (max(p.x_um for p in units) - min(p.x_um for p in units)) + (max(p.y_um for p in units) - min(p.y_um for p in units))
    for group in constraints.symmetry_groups:
        units = [unit for dev in group for unit in by_role.get(dev, ())]
        if len(units) < 2:
            continue
        hpwl += 0.5 * ((max(p.x_um for p in units) - min(p.x_um for p in units)) + (max(p.y_um for p in units) - min(p.y_um for p in units)))
    return hpwl


def _matched_group_y_spread(placements: tuple[Placement, ...], constraints: LayoutConstraintSet) -> float:
    by_role = _placements_by_role(placements)
    spread = 0.0
    for group in constraints.matched_groups:
        ys = [unit.y_um for dev in group.devices for unit in by_role.get(dev, ())]
        if ys:
            spread += max(ys) - min(ys)
    return spread


def _matched_group_centroid_error(placements: tuple[Placement, ...], constraints: LayoutConstraintSet) -> float:
    by_role = _placements_by_role(placements)
    error = 0.0
    for group in constraints.matched_groups:
        role_centroids = []
        for dev in group.devices:
            units = by_role.get(dev, ())
            if units:
                role_centroids.append(sum(unit.x_um for unit in units) / len(units))
        if role_centroids:
            error += max(role_centroids) - min(role_centroids)
    return error


def _placement_constraint_costs(
    placements: tuple[Placement, ...],
    constraints: LayoutConstraintSet,
    *,
    pdk: object | None,
    graph: TopologyGraph | None,
) -> dict[str, float]:
    matched_group_violation_count = 0.0
    matched_group_dummy_violation_count = 0.0
    matched_group_centroid_violation_count = 0.0
    symmetry_group_violation_count = 0.0
    row_policy_violation_count = 0.0

    report = analyze_placement(placements, constraints, pdk=pdk, graph=graph)
    issues = tuple(str(issue) for issue in report["issues"])
    for group in constraints.matched_groups:
        group_issues = tuple(issue for issue in issues if f"matched group {group.name} " in issue)
        if group_issues:
            matched_group_violation_count += 1.0
        if any("missing dummies" in issue for issue in group_issues):
            matched_group_dummy_violation_count += 1.0
        if any("centroid offset" in issue or "role centroid mismatch" in issue for issue in group_issues):
            matched_group_centroid_violation_count += 1.0

    symmetry_tol = float(_analog_placement_profile(pdk).get("symmetry_tolerance_um", 1e-6) or 0.0)
    for group in constraints.symmetry_groups:
        if _symmetry_group_violation_count(placements, group, tolerance_um=symmetry_tol) > 0.0:
            symmetry_group_violation_count += 1.0

    device_role_map = _device_role_map(graph)
    row_tol = float(_analog_placement_profile(pdk).get("row_alignment_tolerance_um", 1e-6) or 0.0)
    row_policy_issues = _placement_role_row_policy_issues(placements, pdk, tolerance_um=row_tol, device_role_map=device_role_map)
    row_policy_violation_count = float(len(row_policy_issues))
    return {
        "matched_group_violations": matched_group_violation_count,
        "matched_group_dummy_violations": matched_group_dummy_violation_count,
        "matched_group_centroid_violations": matched_group_centroid_violation_count,
        "symmetry_group_violations": symmetry_group_violation_count,
        "row_policy_violations": row_policy_violation_count,
    }


def _placement_hierarchy_costs(
    placements: tuple[Placement, ...],
    graph: TopologyGraph | None,
    *,
    pdk: object | None,
    placement_seed_metadata: dict[str, object] | None,
) -> dict[str, float]:
    if graph is None or not placement_seed_metadata:
        return {
            "partition_order_violations": 0.0,
            "anchor_partition_spread": 0.0,
            "focus_partition_separation": 0.0,
            "focus_partition_target_shortfall": 0.0,
            "anchor_partition_target_overflow": 0.0,
            "pcell_partition_internal_spread": 0.0,
            "pex_focus_partition_spread": 0.0,
            "reference_sensitive_partition_spread": 0.0,
            "feedback_sensitive_partition_spread": 0.0,
        }
    partition_device_map = {
        str(name): tuple(str(device) for device in devices if str(device))
        for name, devices in dict(placement_seed_metadata.get("partition_device_map", {})).items()
    }
    if not partition_device_map:
        return {
            "partition_order_violations": 0.0,
            "anchor_partition_spread": 0.0,
            "focus_partition_separation": 0.0,
            "focus_partition_target_shortfall": 0.0,
            "anchor_partition_target_overflow": 0.0,
            "pcell_partition_internal_spread": 0.0,
            "pex_focus_partition_spread": 0.0,
            "reference_sensitive_partition_spread": 0.0,
            "feedback_sensitive_partition_spread": 0.0,
        }
    centers = _placement_centers_by_device(placements)
    partition_centers = _partition_centers_by_name(partition_device_map, centers)
    pcell_partitions = _binding_partition_names_from_metadata(placement_seed_metadata)
    pex_focus_partitions = _parasitic_partition_names_from_metadata(
        placement_seed_metadata,
        predicate=lambda item: bool(item.get("pex_focus_required", False)),
    )
    reference_sensitive_partitions = _parasitic_partition_names_from_metadata(
        placement_seed_metadata,
        predicate=lambda item: bool(tuple(item.get("reference_nets", ()) or ())),
    )
    feedback_sensitive_partitions = _parasitic_partition_names_from_metadata(
        placement_seed_metadata,
        predicate=lambda item: bool(tuple(item.get("feedback_nets", ()) or ())),
    )
    preferred_order = tuple(str(name) for name in placement_seed_metadata.get("preferred_partition_order", ()) if str(name))
    anchor_partitions = tuple(str(name) for name in placement_seed_metadata.get("anchor_partitions", ()) if str(name))
    focus_partitions = tuple(str(name) for name in placement_seed_metadata.get("focus_partitions", ()) if str(name))
    placement_profile = _analog_placement_profile(pdk)
    order_tol = float(placement_profile.get("partition_order_tolerance_um", 1e-6) or 0.0)
    focus_target = float(placement_profile.get("focus_separation_target_um", 0.0) or 0.0)
    anchor_target = float(placement_profile.get("anchor_spread_target_um", 0.0) or 0.0)
    anchor_spread = _partition_spread_cost(partition_centers, anchor_partitions)
    focus_separation = _focus_partition_separation_cost(partition_centers, focus_partitions)
    return {
        "partition_order_violations": _partition_order_violation_cost(partition_centers, preferred_order, tolerance_um=order_tol),
        "anchor_partition_spread": anchor_spread,
        "focus_partition_separation": focus_separation,
        "focus_partition_target_shortfall": max(focus_target - focus_separation, 0.0) if focus_target > 0.0 else 0.0,
        "anchor_partition_target_overflow": max(anchor_spread - anchor_target, 0.0) if anchor_target > 0.0 else 0.0,
        "pcell_partition_internal_spread": _partition_internal_spread_cost(partition_device_map, centers, pcell_partitions),
        "pex_focus_partition_spread": _partition_spread_cost(partition_centers, pex_focus_partitions),
        "reference_sensitive_partition_spread": _partition_spread_cost(partition_centers, reference_sensitive_partitions),
        "feedback_sensitive_partition_spread": _partition_spread_cost(partition_centers, feedback_sensitive_partitions),
    }


def _partition_centers_by_name(
    partition_device_map: dict[str, tuple[str, ...]],
    device_centers: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    centers: dict[str, tuple[float, float]] = {}
    for name, devices in partition_device_map.items():
        points = tuple(device_centers[device] for device in devices if device in device_centers)
        if not points:
            continue
        centers[name] = (
            sum(point[0] for point in points) / len(points),
            sum(point[1] for point in points) / len(points),
        )
    return centers


def _partition_order_violation_cost(
    partition_centers: dict[str, tuple[float, float]],
    preferred_order: tuple[str, ...],
    *,
    tolerance_um: float = 1e-6,
) -> float:
    available = tuple(name for name in preferred_order if name in partition_centers)
    if len(available) < 2:
        return 0.0
    violations = 0.0
    for left, right in zip(available, available[1:]):
        if partition_centers[left][0] > partition_centers[right][0] + tolerance_um:
            violations += 1.0
    return violations


def _partition_spread_cost(
    partition_centers: dict[str, tuple[float, float]],
    partitions: tuple[str, ...],
) -> float:
    available = tuple(name for name in partitions if name in partition_centers)
    if len(available) < 2:
        return 0.0
    xs = [partition_centers[name][0] for name in available]
    ys = [partition_centers[name][1] for name in available]
    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def _partition_internal_spread_cost(
    partition_device_map: Mapping[str, tuple[str, ...]],
    device_centers: Mapping[str, tuple[float, float]],
    partitions: tuple[str, ...],
) -> float:
    total = 0.0
    for name in partitions:
        devices = tuple(str(device) for device in tuple(partition_device_map.get(name, ())) if str(device))
        points = tuple(device_centers[device] for device in devices if device in device_centers)
        if len(points) < 2:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def _binding_partition_names_from_metadata(
    metadata: Mapping[str, object] | None,
) -> tuple[str, ...]:
    if not metadata:
        return ()
    explicit = tuple(str(name) for name in tuple(metadata.get("pcell_sensitive_partitions", ()) or ()) if str(name))
    if explicit:
        return explicit
    plan = dict(metadata.get("hierarchical_partition_pcell_binding_plan", {}) or {})
    return tuple(
        str(item.get("name", ""))
        for item in tuple(plan.get("partitions", ()) or ())
        if isinstance(item, Mapping)
        and str(item.get("name", ""))
        and bool(item.get("pcell_binding_applicable", False))
    )


def _parasitic_partition_names_from_metadata(
    metadata: Mapping[str, object] | None,
    *,
    predicate,
) -> tuple[str, ...]:
    if not metadata:
        return ()
    plan = dict(metadata.get("hierarchical_partition_parasitic_target_plan", {}) or {})
    names: list[str] = []
    for item in tuple(plan.get("partitions", ()) or ()):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", ""))
        if name and predicate(item):
            names.append(name)
    return tuple(names)


def _focus_partition_separation_cost(
    partition_centers: dict[str, tuple[float, float]],
    partitions: tuple[str, ...],
) -> float:
    available = tuple(name for name in partitions if name in partition_centers)
    if len(available) < 2:
        return 0.0
    ordered = sorted(available, key=lambda name: partition_centers[name][0])
    gaps = [
        abs(partition_centers[right][0] - partition_centers[left][0])
        for left, right in zip(ordered, ordered[1:])
    ]
    return max(gaps, default=0.0)


def _symmetry_group_violation_count(
    placements: tuple[Placement, ...],
    group: tuple[str, ...],
    *,
    tolerance_um: float = 1e-6,
) -> float:
    if len(group) != 2:
        return 0.0
    by_role = _placements_by_role(placements)
    left = tuple(by_role.get(group[0], ()))
    right = tuple(by_role.get(group[1], ()))
    if not left or not right:
        return 0.0
    if len(left) != len(right):
        return 1.0
    left_y = sum(unit.y_um for unit in left) / len(left)
    right_y = sum(unit.y_um for unit in right) / len(right)
    if abs(left_y - right_y) > tolerance_um:
        return 1.0
    axis = 0.5 * (
        (sum(unit.x_um for unit in left) / len(left))
        + (sum(unit.x_um for unit in right) / len(right))
    )
    left_offsets = sorted(abs(unit.x_um - axis) for unit in left)
    right_offsets = sorted(abs(unit.x_um - axis) for unit in right)
    if any(abs(a - b) > tolerance_um for a, b in zip(left_offsets, right_offsets)):
        return 1.0
    return 0.0


def _placements_by_role(placements: tuple[Placement, ...]) -> dict[str, tuple[Placement, ...]]:
    by_role: dict[str, list[Placement]] = {}
    for placement in placements:
        by_role.setdefault(placement.role or placement.name, []).append(placement)
        by_role.setdefault(placement.name, []).append(placement)
    return {role: tuple(items) for role, items in by_role.items()}


def _default_pitch_um(graph: TopologyGraph) -> float:
    widths = []
    for device in graph.devices.values():
        value = device.parameters.get("W", device.parameters.get("w", device.parameters.get("width")))
        if isinstance(value, (int, float)) and value > 0:
            widths.append(float(value) * 1e6 if float(value) < 1e-3 else float(value))
    return max(1.0, min(max(widths, default=1.0) * 0.25, 4.0))


def _with_y_offset(placements: tuple[Placement, ...], y_um: float) -> tuple[Placement, ...]:
    return tuple(Placement(p.name, p.x_um, p.y_um + y_um, p.orient, p.role) for p in placements)


def _is_two_stage_miller_ota(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "M2A", "M2B", "MTAIL", "MDRV", "MLOAD", "RZ", "CC"}
    if not required.issubset(graph.devices):
        return False
    roles = {device.role for device in graph.devices.values()}
    net_roles = {net.role for net in graph.nets.values()}
    return (
        DeviceRole.INPUT_PAIR in roles
        and DeviceRole.DRIVER in roles
        and DeviceRole.COMP_RESISTOR in roles
        and DeviceRole.COMP_CAPACITOR in roles
        and NetRole.COMPENSATION in net_roles
        and NetRole.HIGH_Z in net_roles
    )


def _is_three_stage_miller_ota(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "M2A", "M2B", "MTAIL", "M3", "M4", "M5", "M6", "RZ1", "CC1", "CC2"}
    if not required.issubset(graph.devices):
        return False
    roles = {device.role for device in graph.devices.values()}
    net_roles = {net.role for net in graph.nets.values()}
    return (
        DeviceRole.INPUT_PAIR in roles
        and DeviceRole.DRIVER in roles
        and DeviceRole.COMP_RESISTOR in roles
        and DeviceRole.COMP_CAPACITOR in roles
        and NetRole.HIGH_Z in net_roles
        and NetRole.OUTPUT in net_roles
    )


def _is_folded_cascode_ota(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "MTAIL", "MFOLDA", "MFOLDB", "MLOADA", "MLOADB"}
    if not required.issubset(graph.devices):
        return False
    roles = {device.role for device in graph.devices.values()}
    return (
        DeviceRole.INPUT_PAIR in roles
        and DeviceRole.CASCODE in roles
        and DeviceRole.LOAD in roles
        and {"OUTP", "OUTN", "FOLDP", "FOLDN"}.issubset(graph.nets)
    )


def _is_telescopic_ota(graph: TopologyGraph) -> bool:
    required = {"M1A", "M1B", "MTAIL", "M2A", "M2B", "M3A", "M3B", "M4A", "M4B"}
    if not required.issubset(graph.devices):
        return False
    roles = {device.role for device in graph.devices.values()}
    return (
        DeviceRole.INPUT_PAIR in roles
        and DeviceRole.CASCODE in roles
        and DeviceRole.LOAD in roles
        and {"OUTP", "OUTN", "N1P", "N1N", "TOPP", "TOPN"}.issubset(graph.nets)
    )


def _is_pipeline_adc_frontend(graph: TopologyGraph) -> bool:
    required = {
        "REFBUF_P",
        "REFBUF_N",
        "S1_INP",
        "S1_INN",
        "S2_INP",
        "S2_INN",
        "FLASH_INP",
        "FLASH_INN",
        "S1_CAPP",
        "S1_CAPN",
        "S2_CAPP",
        "S2_CAPN",
    }
    return required.issubset(graph.devices)


def _is_reference_buffer(graph: TopologyGraph) -> bool:
    if graph.name.endswith("_reference_buffer"):
        return True
    suffixes = {name.rsplit("_", 1)[-1] for name in graph.devices}
    return {"BUFP", "BUFN", "BIASP", "BIASN"}.issubset(suffixes)


def _is_bandgap_reference(graph: TopologyGraph) -> bool:
    if graph.name.endswith("_brokaw_bandgap"):
        return True
    required_devices = {"Q1", "R1", "M3A", "M3B", "M1A", "M1B", "M5A", "M5B", "M7"}
    required_nets = {"diode1", "diode2", "nR1", "nR2", "ea_out", "TAIL", "BIAS_N", "VDD", "VSS"}
    return required_devices.issubset(graph.devices) and required_nets.issubset(graph.nets)


def _is_pmos_pass_ldo(graph: TopologyGraph) -> bool:
    if graph.name.endswith("_pmos_pass_ldo"):
        return True
    required_devices = {"M1A", "M1B", "M3A", "M3B", "MTAIL", "MPASS", "RFB_TOP", "RFB_BOT", "COUT"}
    required_nets = {"VIN", "VOUT", "VREF", "VFB", "VSS", "TAIL", "EA_REF", "VGATE_PASS"}
    return required_devices.issubset(graph.devices) and required_nets.issubset(graph.nets)


def _is_mdac_stage(graph: TopologyGraph) -> bool:
    if graph.name.endswith("_mdac_stage"):
        return True
    suffixes = {name.rsplit("_", 1)[-1] for name in graph.devices}
    return {"SWP", "SWN", "INP", "INN", "LOADP", "LOADN", "TAIL", "CAPP", "CAPN"}.issubset(suffixes)


def _is_strongarm_comparator(graph: TopologyGraph) -> bool:
    required = {"MCLK"}
    roles = {device.role for device in graph.devices.values()}
    return (
        required.issubset(graph.devices)
        and DeviceRole.INPUT_PAIR in roles
        and DeviceRole.DRIVER in roles
        and DeviceRole.LOAD in roles
        and DeviceRole.TAIL in roles
        and {"OUTP", "OUTN"}.issubset(graph.nets)
    )


def _strongarm_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    del graph
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_tail = -0.5 * row
    y_input = 0.0
    y_latch = 0.55 * row
    y_load = 1.0 * row
    y_reset = 1.5 * row
    return (
        Placement("DUMMY_input_L", -2.5 * p, y_input, role="dummy"),
        Placement("MIN_P_u0", -1.5 * p, y_input, "R0", "MIN_P"),
        Placement("MIN_N_u0", -0.5 * p, y_input, "R0", "MIN_N"),
        Placement("MIN_N_u1", 0.5 * p, y_input, "MY", "MIN_N"),
        Placement("MIN_P_u1", 1.5 * p, y_input, "MY", "MIN_P"),
        Placement("DUMMY_input_R", 2.5 * p, y_input, role="dummy"),
        Placement("MIN_P", -0.9 * p, y_input, "R0", "anchor"),
        Placement("MIN_N", 0.9 * p, y_input, "MY", "anchor"),
        Placement("MCLK", 0.0, y_tail, "R0", "MCLK"),
        Placement("DUMMY_latch_L", -2.5 * p, y_latch, role="dummy"),
        Placement("MLATN_P_u0", -1.5 * p, y_latch, "R0", "MLATN_P"),
        Placement("MLATN_N_u0", -0.5 * p, y_latch, "R0", "MLATN_N"),
        Placement("MLATN_N_u1", 0.5 * p, y_latch, "MY", "MLATN_N"),
        Placement("MLATN_P_u1", 1.5 * p, y_latch, "MY", "MLATN_P"),
        Placement("DUMMY_latch_R", 2.5 * p, y_latch, role="dummy"),
        Placement("MLATN_P", -0.9 * p, y_latch, "R0", "anchor"),
        Placement("MLATN_N", 0.9 * p, y_latch, "MY", "anchor"),
        Placement("DUMMY_load_L", -2.5 * p, y_load, role="dummy"),
        Placement("MLATP_P_u0", -1.5 * p, y_load, "R0", "MLATP_P"),
        Placement("MLATP_N_u0", -0.5 * p, y_load, "R0", "MLATP_N"),
        Placement("MLATP_N_u1", 0.5 * p, y_load, "MY", "MLATP_N"),
        Placement("MLATP_P_u1", 1.5 * p, y_load, "MY", "MLATP_P"),
        Placement("DUMMY_load_R", 2.5 * p, y_load, role="dummy"),
        Placement("MLATP_P", -0.9 * p, y_load, "R0", "anchor"),
        Placement("MLATP_N", 0.9 * p, y_load, "MY", "anchor"),
        Placement("DUMMY_rst_L", -2.5 * p, y_reset, role="dummy"),
        Placement("MRST_P_u0", -1.5 * p, y_reset, "R0", "MRST_P"),
        Placement("MRST_N_u0", -0.5 * p, y_reset, "R0", "MRST_N"),
        Placement("MRST_N_u1", 0.5 * p, y_reset, "MY", "MRST_N"),
        Placement("MRST_P_u1", 1.5 * p, y_reset, "MY", "MRST_P"),
        Placement("DUMMY_rst_R", 2.5 * p, y_reset, role="dummy"),
        Placement("MRST_P", -0.9 * p, y_reset, "R0", "anchor"),
        Placement("MRST_N", 0.9 * p, y_reset, "MY", "anchor"),
    )


def _reference_buffer_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_bias = 0.0
    y_buf = 0.95 * row
    names = tuple(graph.devices)
    biasp = next(name for name in names if name.endswith("_BIASP"))
    biasn = next(name for name in names if name.endswith("_BIASN"))
    bufp = next(name for name in names if name.endswith("_BUFP"))
    bufn = next(name for name in names if name.endswith("_BUFN"))
    return (
        Placement("DUMMY_bias_L", -2.0 * p, y_bias, role="dummy"),
        Placement(biasp, -1.0 * p, y_bias, "R0", biasp),
        Placement(biasn, 1.0 * p, y_bias, "MY", biasn),
        Placement("DUMMY_bias_R", 2.0 * p, y_bias, role="dummy"),
        Placement("DUMMY_buf_L", -2.0 * p, y_buf, role="dummy"),
        Placement(bufp, -1.0 * p, y_buf, "R0", bufp),
        Placement(bufn, 1.0 * p, y_buf, "MY", bufn),
        Placement("DUMMY_buf_R", 2.0 * p, y_buf, role="dummy"),
    )


def _bandgap_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    step = max(float(pitch_um), 1.2)
    amp_pitch = max(0.5, 0.5 * step)
    row = float(row_pitch_um)
    y_tail = 0.5 * row
    y_res = 1.875 * row
    y_bjt = 3.25 * row
    y_amp = 4.625 * row
    y_load = 4.625 * row
    y_mirror = 6.0 * row
    core_x0 = max(3.6, 3.0 * step)
    q2_names = tuple(sorted(name for name in graph.devices if name.startswith("Q2_")))
    r2_names = tuple(sorted(name for name in graph.devices if name.startswith("R2_")))
    placements: list[Placement] = [
        Placement("M3A", core_x0 + 1.5 * step, y_mirror, "R0", "M3A"),
        Placement("M3B", core_x0 + 2.5 * step, y_mirror, "MY", "M3B"),
        Placement("M5A", core_x0 + 2.5 * step, y_load, "MY", "M5A"),
        Placement("M5B", core_x0 + 3.5 * step, y_load, "MY", "M5B"),
        Placement("Q1", core_x0, y_bjt, "R0", "Q1"),
        Placement("R1", core_x0, y_res, "R0", "R1"),
        Placement("M7", core_x0 + 2.0 * step, y_tail, "R0", "M7"),
        Placement("DUMMY_M1A_L", -2.5 * amp_pitch, y_amp, role="dummy"),
        Placement("M1A_u0", -1.5 * amp_pitch, y_amp, "R0", "M1A"),
        Placement("M1B_u0", -0.5 * amp_pitch, y_amp, "R0", "M1B"),
        Placement("M1B_u1", 0.5 * amp_pitch, y_amp, "R0", "M1B"),
        Placement("M1A_u1", 1.5 * amp_pitch, y_amp, "R0", "M1A"),
        Placement("DUMMY_M1B_R", 2.5 * amp_pitch, y_amp, role="dummy"),
    ]
    for idx, name in enumerate(q2_names, start=1):
        placements.append(Placement(name, core_x0 + idx * step, y_bjt, "R0", name))
    for idx, name in enumerate(r2_names, start=1):
        placements.append(Placement(name, core_x0 + idx * step, y_res, "R0", name))
    return tuple(placements)


def _pmos_pass_ldo_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    del graph
    p = max(float(pitch_um), 1.2)
    row = max(float(row_pitch_um), 2.0)
    y_tail = 0.0
    y_input = 1.05 * row
    y_feedback = 1.75 * row
    y_pass = 2.45 * row
    y_load = 3.50 * row
    x_core = 1.5 * p
    x_pass = 5.2 * p
    x_feedback = 5.2 * p
    x_cout = 9.0 * p
    feedback_step = 2.5 * p
    return (
        Placement("MTAIL", x_core + 0.75 * p, y_tail, "R0", "MTAIL"),
        Placement("DUMMY_input_L", x_core - 1.4 * p, y_input, role="dummy"),
        Placement("M1A_u0", x_core - 0.75 * p, y_input, "R0", "M1A"),
        Placement("M1B_u0", x_core + 0.75 * p, y_input, "MY", "M1B"),
        Placement("DUMMY_input_R", x_core + 1.4 * p, y_input, role="dummy"),
        Placement("M1A", x_core - 0.75 * p, y_input, "R0", "anchor"),
        Placement("M1B", x_core + 0.75 * p, y_input, "MY", "anchor"),
        Placement("DUMMY_load_L", x_core - 1.4 * p, y_load, role="dummy"),
        Placement("M3A_u0", x_core - 0.75 * p, y_load, "R0", "M3A"),
        Placement("M3B_u0", x_core + 0.75 * p, y_load, "MY", "M3B"),
        Placement("DUMMY_load_R", x_core + 1.4 * p, y_load, role="dummy"),
        Placement("M3A", x_core - 0.75 * p, y_load, "R0", "anchor"),
        Placement("M3B", x_core + 0.75 * p, y_load, "MY", "anchor"),
        Placement("MPASS", x_pass, y_pass, "MY", "MPASS"),
        Placement("RFB_TOP", x_feedback, y_feedback, "R0", "RFB_TOP"),
        Placement("RFB_BOT", x_feedback + feedback_step, y_feedback, "R0", "RFB_BOT"),
        Placement("COUT", x_cout, y_pass, "R0", "COUT"),
    )


def _mdac_stage_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_switch = 0.0
    y_tail = 0.95 * row
    y_pair = 1.9 * row
    y_load = 2.9 * row
    y_caps = 1.75 * row
    names = tuple(graph.devices)
    swp = next(name for name in names if name.endswith("_SWP"))
    swn = next(name for name in names if name.endswith("_SWN"))
    inp = next(name for name in names if name.endswith("_INP"))
    inn = next(name for name in names if name.endswith("_INN"))
    loadp = next(name for name in names if name.endswith("_LOADP"))
    loadn = next(name for name in names if name.endswith("_LOADN"))
    tail = next(name for name in names if name.endswith("_TAIL"))
    capp = next(name for name in names if name.endswith("_CAPP"))
    capn = next(name for name in names if name.endswith("_CAPN"))
    return (
        Placement("DUMMY_sw_L", -1.75 * p, y_switch, role="dummy"),
        Placement(swp, -0.55 * p, y_switch, "R0", swp),
        Placement(swn, 0.55 * p, y_switch, "MY", swn),
        Placement("DUMMY_sw_R", 1.75 * p, y_switch, role="dummy"),
        Placement("DUMMY_input_L", -2.5 * p, y_pair, role="dummy"),
        Placement(f"{inp}_u0", -1.5 * p, y_pair, "R0", inp),
        Placement(f"{inn}_u0", -0.5 * p, y_pair, "R0", inn),
        Placement(f"{inn}_u1", 0.5 * p, y_pair, "MY", inn),
        Placement(f"{inp}_u1", 1.5 * p, y_pair, "MY", inp),
        Placement("DUMMY_input_R", 2.5 * p, y_pair, role="dummy"),
        Placement(inp, -0.5 * p, y_pair, "R0", "anchor"),
        Placement(inn, 0.5 * p, y_pair, "MY", "anchor"),
        Placement(tail, 0.0, y_tail, "R0", tail),
        Placement("DUMMY_load_L", -1.8 * p, y_load, role="dummy"),
        Placement(loadp, -0.7 * p, y_load, "MY", loadp),
        Placement(loadn, 0.7 * p, y_load, "R0", loadn),
        Placement("DUMMY_load_R", 1.8 * p, y_load, role="dummy"),
        Placement(capp, -2.9 * p, y_caps, "R0", capp),
        Placement(capn, 2.25 * p, y_caps, "R0", capn),
    )


def _two_stage_miller_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    del graph
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_tail = -row
    y_input = 0.0
    y_load = 0.8 * row
    y_output = 0.35 * row
    y_comp = 0.95 * row
    placements = [
        Placement("DUMMY_input_L", -2.5 * p, y_input, role="dummy"),
        Placement("M1A_u0", -1.5 * p, y_input, "R0", "M1A"),
        Placement("M1B_u0", -0.5 * p, y_input, "R0", "M1B"),
        Placement("M1B_u1", 0.5 * p, y_input, "MY", "M1B"),
        Placement("M1A_u1", 1.5 * p, y_input, "MY", "M1A"),
        Placement("DUMMY_input_R", 2.5 * p, y_input, role="dummy"),
        Placement("M1A", -0.75 * p, y_input, "R0", "anchor"),
        Placement("M1B", 0.75 * p, y_input, "MY", "anchor"),
        Placement("MTAIL", 0.0, y_tail, "R0", "MTAIL"),
        Placement("DUMMY_load_L", -2.25 * p, y_load, role="dummy"),
        Placement("M2A_u0", -1.25 * p, y_load, "R0", "M2A"),
        Placement("M2B_u0", 1.25 * p, y_load, "MY", "M2B"),
        Placement("DUMMY_load_R", 2.25 * p, y_load, role="dummy"),
        Placement("M2A", -1.25 * p, y_load, "R0", "anchor"),
        Placement("M2B", 1.25 * p, y_load, "MY", "anchor"),
        Placement("MDRV", 1.85 * p, y_output, "R0", "MDRV"),
        Placement("MLOAD", 1.85 * p, y_load, "R0", "MLOAD"),
        Placement("RZ", 1.25 * p, y_comp, "R90", "RZ"),
        Placement("CC", 2.2 * p, y_comp, "R0", "CC"),
    ]
    return tuple(placements)


def _three_stage_miller_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    del graph
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_tail = -row
    y_input = 0.0
    y_stage2 = row
    y_stage3 = 2.0 * row
    y_load = 3.0 * row
    y_comp = 1.5 * row
    return (
        Placement("DUMMY_input_L", -2.5 * p, y_input, role="dummy"),
        Placement("M1A_u0", -1.5 * p, y_input, "R0", "M1A"),
        Placement("M1B_u0", -0.5 * p, y_input, "R0", "M1B"),
        Placement("M1B_u1", 0.5 * p, y_input, "MY", "M1B"),
        Placement("M1A_u1", 1.5 * p, y_input, "MY", "M1A"),
        Placement("DUMMY_input_R", 2.5 * p, y_input, role="dummy"),
        Placement("M1A", -0.75 * p, y_input, "R0", "anchor"),
        Placement("M1B", 0.75 * p, y_input, "MY", "anchor"),
        Placement("MTAIL", 0.0, y_tail, "R0", "MTAIL"),
        Placement("M2A", -1.0 * p, y_load, "R0", "M2A"),
        Placement("M2B", 1.0 * p, y_load, "MY", "M2B"),
        Placement("M3", 0.0, y_stage2, "R0", "M3"),
        Placement("M4", 0.0, y_load, "MY", "M4"),
        Placement("M5", 3.0 * p, y_stage3, "R0", "M5"),
        Placement("M6", 3.0 * p, y_load, "MY", "M6"),
        Placement("RZ1", 1.5 * p, y_comp, "R0", "RZ1"),
        # Keep the compensation capacitors clear of the M5/M6 output stack and
        # leave enough horizontal room so their drawn primitive plates do not
        # overlap each other or the resistor terminal access shapes.
        Placement("CC1", 3.8 * p, y_comp, "R0", "CC1"),
        Placement("CC2", 5.2 * p, y_comp + 0.25 * row, "R0", "CC2"),
    )


def _folded_cascode_ota_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    del graph
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_fold = 0.0
    y_input = 0.9 * row
    y_tail = 1.8 * row
    y_load = 2.7 * row
    return (
        Placement("DUMMY_fold_L", -2.25 * p, y_fold, role="dummy"),
        Placement("MFOLDA_u0", -1.25 * p, y_fold, "R0", "MFOLDA"),
        Placement("MFOLDB_u0", 1.25 * p, y_fold, "MY", "MFOLDB"),
        Placement("DUMMY_fold_R", 2.25 * p, y_fold, role="dummy"),
        Placement("MFOLDA", -1.25 * p, y_fold, "R0", "anchor"),
        Placement("MFOLDB", 1.25 * p, y_fold, "MY", "anchor"),
        Placement("DUMMY_input_L", -2.5 * p, y_input, role="dummy"),
        Placement("M1A_u0", -1.5 * p, y_input, "R0", "M1A"),
        Placement("M1B_u0", -0.5 * p, y_input, "R0", "M1B"),
        Placement("M1B_u1", 0.5 * p, y_input, "MY", "M1B"),
        Placement("M1A_u1", 1.5 * p, y_input, "MY", "M1A"),
        Placement("DUMMY_input_R", 2.5 * p, y_input, role="dummy"),
        Placement("M1A", -0.75 * p, y_input, "R0", "anchor"),
        Placement("M1B", 0.75 * p, y_input, "MY", "anchor"),
        Placement("MTAIL", 0.0, y_tail, "R0", "MTAIL"),
        Placement("DUMMY_load_L", -2.25 * p, y_load, role="dummy"),
        Placement("MLOADA_u0", -1.25 * p, y_load, "R0", "MLOADA"),
        Placement("MLOADB_u0", 1.25 * p, y_load, "MY", "MLOADB"),
        Placement("DUMMY_load_R", 2.25 * p, y_load, role="dummy"),
        Placement("MLOADA", -1.25 * p, y_load, "R0", "anchor"),
        Placement("MLOADB", 1.25 * p, y_load, "MY", "anchor"),
    )


def _telescopic_ota_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    del graph
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_tail = -0.9 * row
    y_input = 0.0
    y_ncas = 0.95 * row
    y_pcas = 1.95 * row
    y_pload = 2.85 * row
    return (
        Placement("DUMMY_input_L", -2.5 * p, y_input, role="dummy"),
        Placement("M1A_u0", -1.5 * p, y_input, "R0", "M1A"),
        Placement("M1B_u0", -0.5 * p, y_input, "R0", "M1B"),
        Placement("M1B_u1", 0.5 * p, y_input, "MY", "M1B"),
        Placement("M1A_u1", 1.5 * p, y_input, "MY", "M1A"),
        Placement("DUMMY_input_R", 2.5 * p, y_input, role="dummy"),
        Placement("M1A", -0.75 * p, y_input, "R0", "anchor"),
        Placement("M1B", 0.75 * p, y_input, "MY", "anchor"),
        Placement("MTAIL", 0.0, y_tail, "R0", "MTAIL"),
        Placement("DUMMY_ncas_L", -2.25 * p, y_ncas, role="dummy"),
        Placement("M2A_u0", -1.25 * p, y_ncas, "R0", "M2A"),
        Placement("M2B_u0", 1.25 * p, y_ncas, "MY", "M2B"),
        Placement("DUMMY_ncas_R", 2.25 * p, y_ncas, role="dummy"),
        Placement("M2A", -1.25 * p, y_ncas, "R0", "anchor"),
        Placement("M2B", 1.25 * p, y_ncas, "MY", "anchor"),
        Placement("DUMMY_pcas_L", -2.25 * p, y_pcas, role="dummy"),
        Placement("M4A_u0", -1.25 * p, y_pcas, "R0", "M4A"),
        Placement("M4B_u0", 1.25 * p, y_pcas, "MY", "M4B"),
        Placement("DUMMY_pcas_R", 2.25 * p, y_pcas, role="dummy"),
        Placement("M4A", -1.25 * p, y_pcas, "R0", "anchor"),
        Placement("M4B", 1.25 * p, y_pcas, "MY", "anchor"),
        Placement("DUMMY_pload_L", -2.25 * p, y_pload, role="dummy"),
        Placement("M3A_u0", -1.25 * p, y_pload, "R0", "M3A"),
        Placement("M3B_u0", 1.25 * p, y_pload, "MY", "M3B"),
        Placement("DUMMY_pload_R", 2.25 * p, y_pload, role="dummy"),
        Placement("M3A", -1.25 * p, y_pload, "R0", "anchor"),
        Placement("M3B", 1.25 * p, y_pload, "MY", "anchor"),
    )


def _pipeline_adc_frontend_seed_placement(graph: TopologyGraph, pitch_um: float, row_pitch_um: float) -> tuple[Placement, ...]:
    del graph
    p = float(pitch_um)
    row = float(row_pitch_um)
    y_tail = -row
    y_switch = 0.0
    y_pair = row
    y_caps = row
    y_load = 3.0 * row
    y_ref = 2.0 * row
    ref_center = -8.0 * p
    s1_center = -4.0 * p
    s1_cap_center = -1.2 * p
    s2_cap_center = 1.2 * p
    s2_center = 4.0 * p
    flash_center = 10.0 * p
    return (
        Placement("DUMMY_refbuf_L", ref_center - 1.5 * p, y_ref, role="dummy"),
        Placement("REFBUF_P_u0", ref_center - 0.5 * p, y_ref, "R0", "REFBUF_P"),
        Placement("REFBUF_N_u0", ref_center + 0.5 * p, y_ref, "MY", "REFBUF_N"),
        Placement("DUMMY_refbuf_R", ref_center + 1.5 * p, y_ref, role="dummy"),
        Placement("REFBUF_P", ref_center - 0.5 * p, y_ref, "R0", "anchor"),
        Placement("REFBUF_N", ref_center + 0.5 * p, y_ref, "MY", "anchor"),
        Placement("REFBIAS_P", ref_center - 0.5 * p, y_tail, "R0", "REFBIAS_P"),
        Placement("REFBIAS_N", ref_center + 0.5 * p, y_tail, "MY", "REFBIAS_N"),
        Placement("S1_SWP", s1_center - 0.5 * p, y_switch, "R0", "S1_SWP"),
        Placement("S1_SWN", s1_center + 0.5 * p, y_switch, "MY", "S1_SWN"),
        Placement("DUMMY_s1_input_L", s1_center - 2.5 * p, y_pair, role="dummy"),
        Placement("S1_INP_u0", s1_center - 1.5 * p, y_pair, "R0", "S1_INP"),
        Placement("S1_INN_u0", s1_center - 0.5 * p, y_pair, "R0", "S1_INN"),
        Placement("S1_INN_u1", s1_center + 0.5 * p, y_pair, "MY", "S1_INN"),
        Placement("S1_INP_u1", s1_center + 1.5 * p, y_pair, "MY", "S1_INP"),
        Placement("DUMMY_s1_input_R", s1_center + 2.5 * p, y_pair, role="dummy"),
        Placement("S1_INP", s1_center - 0.5 * p, y_pair, "R0", "anchor"),
        Placement("S1_INN", s1_center + 0.5 * p, y_pair, "MY", "anchor"),
        Placement("S1_LOADP", s1_center - 0.5 * p, y_load, "MY", "S1_LOADP"),
        Placement("S1_LOADN", s1_center + 0.5 * p, y_load, "R0", "S1_LOADN"),
        Placement("S1_TAIL", s1_center, y_tail, "R0", "S1_TAIL"),
        Placement("DUMMY_s1_cap_L", s1_cap_center - 2.5 * p, y_caps, role="dummy"),
        Placement("S1_CAPP_u0", s1_cap_center - 1.5 * p, y_caps, "R0", "S1_CAPP"),
        Placement("S1_CAPN_u0", s1_cap_center - 0.5 * p, y_caps, "R0", "S1_CAPN"),
        Placement("S1_CAPN_u1", s1_cap_center + 0.5 * p, y_caps, "MY", "S1_CAPN"),
        Placement("S1_CAPP_u1", s1_cap_center + 1.5 * p, y_caps, "MY", "S1_CAPP"),
        Placement("DUMMY_s1_cap_R", s1_cap_center + 2.5 * p, y_caps, role="dummy"),
        Placement("S1_CAPP", s1_cap_center - 0.5 * p, y_caps, "R0", "anchor"),
        Placement("S1_CAPN", s1_cap_center + 0.5 * p, y_caps, "MY", "anchor"),
        Placement("S2_SWP", s2_center - 0.5 * p, y_switch, "R0", "S2_SWP"),
        Placement("S2_SWN", s2_center + 0.5 * p, y_switch, "MY", "S2_SWN"),
        Placement("DUMMY_s2_input_L", s2_center - 2.5 * p, y_pair, role="dummy"),
        Placement("S2_INP_u0", s2_center - 1.5 * p, y_pair, "R0", "S2_INP"),
        Placement("S2_INN_u0", s2_center - 0.5 * p, y_pair, "R0", "S2_INN"),
        Placement("S2_INN_u1", s2_center + 0.5 * p, y_pair, "MY", "S2_INN"),
        Placement("S2_INP_u1", s2_center + 1.5 * p, y_pair, "MY", "S2_INP"),
        Placement("DUMMY_s2_input_R", s2_center + 2.5 * p, y_pair, role="dummy"),
        Placement("S2_INP", s2_center - 0.5 * p, y_pair, "R0", "anchor"),
        Placement("S2_INN", s2_center + 0.5 * p, y_pair, "MY", "anchor"),
        Placement("S2_LOADP", s2_center - 0.5 * p, y_load, "MY", "S2_LOADP"),
        Placement("S2_LOADN", s2_center + 0.5 * p, y_load, "R0", "S2_LOADN"),
        Placement("S2_TAIL", s2_center, y_tail, "R0", "S2_TAIL"),
        Placement("DUMMY_s2_cap_L", s2_cap_center - 2.5 * p, y_caps, role="dummy"),
        Placement("S2_CAPP_u0", s2_cap_center - 1.5 * p, y_caps, "R0", "S2_CAPP"),
        Placement("S2_CAPN_u0", s2_cap_center - 0.5 * p, y_caps, "R0", "S2_CAPN"),
        Placement("S2_CAPN_u1", s2_cap_center + 0.5 * p, y_caps, "MY", "S2_CAPN"),
        Placement("S2_CAPP_u1", s2_cap_center + 1.5 * p, y_caps, "MY", "S2_CAPP"),
        Placement("DUMMY_s2_cap_R", s2_cap_center + 2.5 * p, y_caps, role="dummy"),
        Placement("S2_CAPP", s2_cap_center - 0.5 * p, y_caps, "R0", "anchor"),
        Placement("S2_CAPN", s2_cap_center + 0.5 * p, y_caps, "MY", "anchor"),
        Placement("DUMMY_flash_input_L", flash_center - 2.5 * p, y_pair, role="dummy"),
        Placement("FLASH_INP_u0", flash_center - 1.5 * p, y_pair, "R0", "FLASH_INP"),
        Placement("FLASH_INN_u0", flash_center - 0.5 * p, y_pair, "R0", "FLASH_INN"),
        Placement("FLASH_INN_u1", flash_center + 0.5 * p, y_pair, "MY", "FLASH_INN"),
        Placement("FLASH_INP_u1", flash_center + 1.5 * p, y_pair, "MY", "FLASH_INP"),
        Placement("DUMMY_flash_input_R", flash_center + 2.5 * p, y_pair, role="dummy"),
        Placement("FLASH_INP", flash_center - 0.5 * p, y_pair, "R0", "anchor"),
        Placement("FLASH_INN", flash_center + 0.5 * p, y_pair, "MY", "anchor"),
        Placement("FLASH_LOADP", flash_center - 0.5 * p, y_load, "MY", "FLASH_LOADP"),
        Placement("FLASH_LOADN", flash_center + 0.5 * p, y_load, "R0", "FLASH_LOADN"),
        Placement("FLASH_TAIL", flash_center, y_tail, "R0", "FLASH_TAIL"),
    )


def _mirror_group_placement(name: str, devices: tuple[str, ...], pitch_um: float, y_um: float, *, include_dummies: bool) -> tuple[Placement, ...]:
    sequence = list(devices)
    if include_dummies:
        sequence = [f"DUMMY_{name}_L", *sequence, f"DUMMY_{name}_R"]
    center = (len(sequence) - 1) / 2
    placements: list[Placement] = []
    for idx, item in enumerate(sequence):
        role = "dummy" if item.startswith("DUMMY_") else item
        orient = "R0" if idx <= center else "MY"
        placements.append(Placement(item, (idx - center) * pitch_um, y_um, orient, role))
    return tuple(placements)


def _apply_symmetry_groups(
    placements: tuple[Placement, ...],
    symmetry_groups: tuple[tuple[str, ...], ...],
    *,
    symmetry_axis: str = "y",
) -> tuple[Placement, ...]:
    result = list(placements)
    for group in symmetry_groups:
        if len(group) != 2:
            continue
        left_name, right_name = group
        left_indices = [idx for idx, p in enumerate(result) if p.role == left_name or p.name == left_name]
        right_indices = [idx for idx, p in enumerate(result) if p.role == right_name or p.name == right_name]
        if not left_indices or not right_indices:
            continue
        if symmetry_axis == "x":
            left_y = sum(result[idx].y_um for idx in left_indices) / len(left_indices)
            right_y = sum(result[idx].y_um for idx in right_indices) / len(right_indices)
            axis = (left_y + right_y) / 2
            for idx in left_indices:
                p = result[idx]
                result[idx] = Placement(p.name, p.x_um, axis - abs(p.y_um - axis), p.orient, p.role)
            for idx in right_indices:
                p = result[idx]
                result[idx] = Placement(p.name, p.x_um, axis + abs(p.y_um - axis), "MX" if p.orient == "R0" else p.orient, p.role)
        else:
            left_x = sum(result[idx].x_um for idx in left_indices) / len(left_indices)
            right_x = sum(result[idx].x_um for idx in right_indices) / len(right_indices)
            axis = (left_x + right_x) / 2
            for idx in left_indices:
                p = result[idx]
                result[idx] = Placement(p.name, axis - abs(p.x_um - axis), p.y_um, p.orient, p.role)
            for idx in right_indices:
                p = result[idx]
                result[idx] = Placement(p.name, axis + abs(p.x_um - axis), p.y_um, "MY" if p.orient == "R0" else p.orient, p.role)
    return tuple(result)


def _apply_role_orient_policy(
    placements: tuple[Placement, ...],
    pdk: object | None,
    *,
    device_role_map: dict[str, DeviceRole] | None = None,
) -> tuple[Placement, ...]:
    site = getattr(pdk, "placement_site", None)
    if site is None:
        return placements
    policy = dict(getattr(site, "role_orient_policy", {}))
    if not policy:
        return placements
    result: list[Placement] = []
    for placement in placements:
        allowed = _lookup_placement_policy(policy, placement, device_role_map)
        if not allowed:
            result.append(placement)
            continue
        orient = placement.orient if placement.orient in allowed else allowed[0]
        result.append(Placement(placement.name, placement.x_um, placement.y_um, orient, placement.role))
    return tuple(result)


def _apply_role_row_policy(
    placements: tuple[Placement, ...],
    pdk: object | None,
    *,
    device_role_map: dict[str, DeviceRole] | None = None,
) -> tuple[Placement, ...]:
    site = getattr(pdk, "placement_site", None)
    if site is None:
        return placements
    policy = dict(getattr(site, "role_row_policy", {}))
    if not policy:
        return placements
    ys = [placement.y_um for placement in placements if placement.role != "dummy"]
    if not ys:
        return placements
    y_min = min(ys)
    y_max = max(ys)
    y_mid = 0.5 * (y_min + y_max)
    row_pitch = float(getattr(site, "row_pitch_um", 2.0) or 2.0)
    result: list[Placement] = []
    for placement in placements:
        row_rule = _lookup_placement_policy(policy, placement, device_role_map)
        if not row_rule or row_rule in {"any", "shared"}:
            result.append(placement)
            continue
        if _role_spans_multiple_bands(placements, placement, device_role_map=device_role_map, row_pitch=row_pitch):
            result.append(placement)
            continue
        target_y = _target_row_y(row_rule, placement.y_um, y_min, y_mid, y_max, row_pitch)
        result.append(Placement(placement.name, placement.x_um, target_y, placement.orient, placement.role))
    return tuple(result)


def _placement_role_row_policy_issues(
    placements: tuple[Placement, ...],
    pdk: object | None,
    *,
    tolerance_um: float = 1e-6,
    device_role_map: dict[str, DeviceRole] | None = None,
) -> tuple[str, ...]:
    site = getattr(pdk, "placement_site", None)
    if site is None:
        return ()
    policy = dict(getattr(site, "role_row_policy", {}))
    if not policy:
        return ()
    by_role = _placements_by_role(placements)
    ys = [placement.y_um for placement in placements if placement.role != "dummy"]
    if not ys:
        return ()
    y_min = min(ys)
    y_max = max(ys)
    y_mid = 0.5 * (y_min + y_max)
    issues: list[str] = []
    for role, row_rule in policy.items():
        units = tuple(
            placement
            for placement in placements
            if role in _placement_policy_keys(placement, device_role_map)
        )
        if not units or row_rule in {"any", "shared"}:
            continue
        if _units_span_multiple_bands(units, row_pitch=float(getattr(site, "row_pitch_um", 2.0) or 2.0)):
            continue
        values = [unit.y_um for unit in units]
        avg_y = sum(values) / len(values)
        if row_rule == "bottom" and abs(avg_y - y_min) > tolerance_um:
            issues.append(f"role {role} violates bottom-row policy")
        elif row_rule == "top" and abs(avg_y - y_max) > tolerance_um:
            issues.append(f"role {role} violates top-row policy")
        elif row_rule == "upper_mid" and avg_y + tolerance_um < y_mid:
            issues.append(f"role {role} violates upper-mid row policy")
        elif row_rule == "lower_mid" and avg_y - tolerance_um > y_mid:
            issues.append(f"role {role} violates lower-mid row policy")
    return tuple(issues)


def _analog_placement_profile(pdk: object | None) -> dict[str, float]:
    profile = getattr(pdk, "analog_placement_constraints", None)
    if profile is None:
        return {
            "match_tolerance_um": 1e-6,
            "symmetry_tolerance_um": 1e-6,
            "row_alignment_tolerance_um": 1e-6,
            "partition_order_tolerance_um": 1e-6,
            "focus_separation_target_um": 0.0,
            "anchor_spread_target_um": 0.0,
        }
    return {
        "match_tolerance_um": float(getattr(profile, "match_tolerance_um", 1e-6) or 0.0),
        "symmetry_tolerance_um": float(getattr(profile, "symmetry_tolerance_um", 1e-6) or 0.0),
        "row_alignment_tolerance_um": float(getattr(profile, "row_alignment_tolerance_um", 1e-6) or 0.0),
        "partition_order_tolerance_um": float(getattr(profile, "partition_order_tolerance_um", 1e-6) or 0.0),
        "focus_separation_target_um": float(getattr(profile, "focus_separation_target_um", 0.0) or 0.0),
        "anchor_spread_target_um": float(getattr(profile, "anchor_spread_target_um", 0.0) or 0.0),
    }


def _role_spans_multiple_bands(
    placements: tuple[Placement, ...],
    target: Placement,
    *,
    device_role_map: dict[str, DeviceRole] | None,
    row_pitch: float,
) -> bool:
    target_keys = set(_placement_policy_keys(target, device_role_map))
    if not target_keys:
        return False
    units = tuple(
        placement
        for placement in placements
        if target_keys.intersection(_placement_policy_keys(placement, device_role_map))
    )
    return _units_span_multiple_bands(units, row_pitch=row_pitch)


def _units_span_multiple_bands(
    units: Sequence[Placement],
    *,
    row_pitch: float,
) -> bool:
    ys = [float(unit.y_um) for unit in units if unit.role != "dummy"]
    if len(ys) < 2:
        return False
    return (max(ys) - min(ys)) > max(0.4 * row_pitch, 1e-6)


def _lookup_placement_policy(
    policy: dict[str, tuple[str, ...]] | dict[str, str],
    placement: Placement,
    device_role_map: dict[str, DeviceRole] | None,
) -> tuple[str, ...] | str | None:
    for key in _placement_policy_keys(placement, device_role_map):
        if key in policy:
            return policy[key]
    return None


def _placement_policy_keys(placement: Placement, device_role_map: dict[str, DeviceRole] | None) -> tuple[str, ...]:
    keys: list[str] = []
    for identity in (placement.role, placement.name, _unit_parent_name(placement.name)):
        if not identity:
            continue
        _append_policy_key_variants(keys, str(identity))
        role = (device_role_map or {}).get(str(identity))
        if role is not None:
            _append_policy_key_variants(keys, role.name)
            _append_policy_key_variants(keys, role.value)
    return tuple(dict.fromkeys(keys))


def _append_policy_key_variants(keys: list[str], value: str) -> None:
    if not value:
        return
    keys.extend((value, value.upper(), value.lower()))


def _placement_device_name(placement: Placement) -> str:
    role = str(placement.role or "")
    if role and role != "dummy":
        return role
    if placement.name.startswith("DUMMY_"):
        return ""
    parent = _unit_parent_name(placement.name)
    if parent:
        return parent
    return placement.name


def _dominant_orient(placements: Sequence[Placement]) -> str:
    counts: dict[str, int] = {}
    for placement in placements:
        orient = str(placement.orient or "R0")
        counts[orient] = counts.get(orient, 0) + 1
    if not counts:
        return "R0"
    return max(counts.items(), key=lambda item: (item[1], item[0] == "R0"))[0]


def _unit_parent_name(name: str) -> str:
    base, sep, suffix = name.rpartition("_u")
    if sep and suffix.isdigit():
        return base
    return ""


def _device_role_map(graph: TopologyGraph | None) -> dict[str, DeviceRole]:
    if graph is None:
        return {}
    return {device.name: device.role for device in graph.devices.values()}


def _target_row_y(
    row_rule: str,
    current_y: float,
    y_min: float,
    y_mid: float,
    y_max: float,
    row_pitch: float,
) -> float:
    if row_rule == "bottom":
        return y_min
    if row_rule == "top":
        return y_min + 2.0 * row_pitch if abs(y_max - y_min) <= 1e-6 else y_max
    if row_rule == "upper_mid":
        return y_min + row_pitch if abs(y_max - y_min) <= 1e-6 else max(y_mid, y_min + row_pitch)
    if row_rule == "lower_mid":
        if abs(y_max - y_min) <= 1e-6:
            return y_min + row_pitch
        return min(y_mid, y_max - row_pitch)
    return current_y
