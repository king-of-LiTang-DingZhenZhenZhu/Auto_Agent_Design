"""Local access candidate enumeration and SMT legalization for native stdcells."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from analogskills.contracts import TopologyGraph
from analogskills.layout.physical import BBox, bbox_overlaps
from analogskills.layout.stdcell_access import native_stdcell_sd_access_side, native_stdcell_terminal_bbox

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None

if TYPE_CHECKING:
    from .stdcell_primitives import NativeStdCellAccessCatalog, NativeStdCellFloorplan
    from analogskills.pdk import PdkConfig


@dataclass(frozen=True)
class NativeStdCellLocalAccessAnchor:
    instance: str
    terminal: str
    net: str
    xy: tuple[float, float]


@dataclass(frozen=True)
class NativeStdCellAccessCandidate:
    instance: str
    terminal: str
    net: str
    xy: tuple[float, float]
    landing_bbox_um: BBox
    source: str
    cost: int = 0


def enumerate_native_stdcell_sd_access_candidates(
    access_catalog: "NativeStdCellAccessCatalog",
    floorplan: "NativeStdCellFloorplan",
    pdk: "PdkConfig",
    *,
    instance: str,
    terminal: str,
    net: str,
) -> tuple[NativeStdCellAccessCandidate, ...]:
    bbox_um = native_stdcell_terminal_bbox(access_catalog, instance, terminal)
    breakout = access_catalog.breakout_for(instance, terminal)
    via_margin = _via0_landing_margin_um(pdk)
    candidates: list[NativeStdCellAccessCandidate] = []
    seen_xy: set[tuple[float, float]] = set()

    def add_candidate(xy: tuple[float, float], source: str, cost: int) -> None:
        snapped_xy = tuple(float(v) for v in pdk.rules.snap_point_um(xy))
        if snapped_xy in seen_xy:
            return
        seen_xy.add(snapped_xy)
        adjusted_cost = int(cost) + _reference_sd_candidate_cost_adjustment(
            floorplan,
            net=str(net),
            xy=snapped_xy,
        )
        landing = pdk.rules.snap_bbox_um(
            (
                snapped_xy[0] - via_margin,
                snapped_xy[1] - via_margin,
                snapped_xy[0] + via_margin,
                snapped_xy[1] + via_margin,
            ),
            mode="outward",
        )
        candidates.append(
            NativeStdCellAccessCandidate(
                instance=str(instance),
                terminal=str(terminal),
                net=str(net),
                xy=snapped_xy,
                landing_bbox_um=landing,
                source=str(source),
                cost=adjusted_cost,
            )
        )

    if bbox_um is not None:
        preferred = native_stdcell_sd_access_side(floorplan, instance, terminal)
        center_y = (bbox_um[1] + bbox_um[3]) / 2.0
        left_xy = (bbox_um[0] + via_margin, center_y)
        right_xy = (bbox_um[2] - via_margin, center_y)
        if preferred == "left":
            add_candidate(left_xy, "bbox_preferred", 0)
            add_candidate(tuple(float(v) for v in getattr(breakout, "xy_um")), "breakout", 1)
            add_candidate(right_xy, "bbox_alternate", 2)
        else:
            add_candidate(right_xy, "bbox_preferred", 0)
            add_candidate(tuple(float(v) for v in getattr(breakout, "xy_um")), "breakout", 1)
            add_candidate(left_xy, "bbox_alternate", 2)
    else:
        add_candidate(tuple(float(v) for v in getattr(breakout, "xy_um")), "breakout", 0)
    return tuple(candidates)


def enumerate_native_stdcell_gate_access_candidates(
    access_catalog: "NativeStdCellAccessCatalog",
    pdk: "PdkConfig",
    *,
    instance: str,
    terminal: str,
    net: str,
    target_y: float | None = None,
) -> tuple[NativeStdCellAccessCandidate, ...]:
    pins = tuple(pin for pin in access_catalog.pins_for(instance, terminal) if str(getattr(pin, "layer", "")) == "PO")
    if not pins:
        breakout = access_catalog.breakout_for(instance, terminal)
        pins = (breakout,) if str(getattr(breakout, "layer", "")) == "PO" else ()
    if not pins:
        return ()

    snapped_target_y = None if target_y is None else float(pdk.rules.snap_point_um((0.0, target_y))[1])
    ordered = sorted(
        pins,
        key=lambda pin: (
            0 if bool(getattr(pin, "lvs_safe", True)) else 1,
            abs(float(getattr(pin, "xy_um")[1]) - snapped_target_y) if snapped_target_y is not None else 0.0,
            int(getattr(pin, "access_priority", 50)),
            str(getattr(pin, "source", "")),
        ),
    )
    candidates: list[NativeStdCellAccessCandidate] = []
    seen_xy: set[tuple[float, float]] = set()
    for rank, pin in enumerate(ordered):
        xy = tuple(float(v) for v in pdk.rules.snap_point_um(getattr(pin, "xy_um")))
        if xy in seen_xy:
            continue
        seen_xy.add(xy)
        bbox_um = getattr(pin, "bbox_um", None)
        if bbox_um is None:
            half = max(_via0_landing_margin_um(pdk), 0.015)
            bbox_um = (xy[0] - half, xy[1] - half, xy[0] + half, xy[1] + half)
        landing = pdk.rules.snap_bbox_um(tuple(float(v) for v in bbox_um), mode="outward")
        candidates.append(
            NativeStdCellAccessCandidate(
                instance=str(instance),
                terminal=str(terminal),
                net=str(net),
                xy=xy,
                landing_bbox_um=landing,
                source=str(getattr(pin, "source", "gate_pin")),
                cost=(100 if not bool(getattr(pin, "lvs_safe", True)) else 0) + rank,
            )
        )
    return tuple(candidates)


def solve_native_stdcell_local_accesses(
    anchors: tuple[NativeStdCellLocalAccessAnchor, ...],
    graph: TopologyGraph,
    floorplan: "NativeStdCellFloorplan",
    access_catalog: "NativeStdCellAccessCatalog",
    pdk: "PdkConfig",
) -> dict[tuple[str, str, str], NativeStdCellAccessCandidate]:
    if not anchors:
        return {}
    if z3 is None:
        return {}

    requests = tuple(
        (
            anchor,
            enumerate_native_stdcell_sd_access_candidates(
                access_catalog,
                floorplan,
                pdk,
                instance=anchor.instance,
                terminal=anchor.terminal,
                net=anchor.net,
            ),
        )
        for anchor in anchors
    )
    if any(not candidates for _, candidates in requests):
        return {}

    solver = z3.Optimize()
    choice_vars: dict[tuple[str, str, str], object] = {}
    objective_terms: list[object] = []
    obstacles = _collect_m0_obstacles(graph, access_catalog)
    request_map = {
        (anchor.instance, anchor.terminal, anchor.net): (anchor, candidates)
        for anchor, candidates in requests
    }

    for anchor, candidates in requests:
        key = (anchor.instance, anchor.terminal, anchor.net)
        choice = z3.Int(f"stdcell_local_access_{anchor.instance}_{anchor.terminal}_{anchor.net}")
        solver.add(z3.Or([choice == idx for idx in range(len(candidates))]))
        valid_indices = [
            idx
            for idx, candidate in enumerate(candidates)
            if _candidate_is_obstacle_safe(candidate, anchor.net, obstacles)
        ]
        if not valid_indices:
            return {}
        solver.add(z3.Or([choice == idx for idx in valid_indices]))
        choice_vars[key] = choice
        objective_terms.append(z3.Sum([z3.If(choice == idx, candidate.cost, 0) for idx, candidate in enumerate(candidates)]))

    keys = tuple(choice_vars)
    for index, left_key in enumerate(keys):
        left_choice = choice_vars[left_key]
        left_anchor, left_cands = request_map[left_key]
        for right_key in keys[index + 1 :]:
            right_anchor, right_cands = request_map[right_key]
            if left_anchor.net == right_anchor.net:
                continue
            allowed_pairs = [
                z3.And(left_choice == li, choice_vars[right_key] == ri)
                for li, left_candidate in enumerate(left_cands)
                for ri, right_candidate in enumerate(right_cands)
                if not bbox_overlaps(left_candidate.landing_bbox_um, right_candidate.landing_bbox_um, include_touching=True)
            ]
            if not allowed_pairs:
                return {}
            solver.add(z3.Or(allowed_pairs))

    solver.minimize(z3.Sum(objective_terms))
    if solver.check() != z3.sat:
        return {}
    model = solver.model()
    selected: dict[tuple[str, str, str], NativeStdCellAccessCandidate] = {}
    for anchor, candidates in requests:
        key = (anchor.instance, anchor.terminal, anchor.net)
        idx = model.eval(choice_vars[key]).as_long()
        selected[key] = candidates[idx]
    return selected


def _collect_m0_obstacles(
    graph: TopologyGraph,
    access_catalog: "NativeStdCellAccessCatalog",
) -> tuple[tuple[str, str, str, BBox], ...]:
    obstacles: list[tuple[str, str, str, BBox]] = []
    for net_name, net in graph.nets.items():
        for terminal_ref in net.terminals:
            if terminal_ref.device not in graph.devices or terminal_ref.terminal not in {"S", "D"}:
                continue
            bbox_um = native_stdcell_terminal_bbox(access_catalog, terminal_ref.device, terminal_ref.terminal)
            if bbox_um is None:
                continue
            obstacles.append((str(net_name), str(terminal_ref.device), str(terminal_ref.terminal), bbox_um))
    return tuple(obstacles)


def _candidate_is_obstacle_safe(
    candidate: NativeStdCellAccessCandidate,
    net_name: str,
    obstacles: tuple[tuple[str, str, str, BBox], ...],
) -> bool:
    for obstacle_net, obstacle_inst, obstacle_term, obstacle_bbox in obstacles:
        if obstacle_net == net_name:
            continue
        if obstacle_inst == candidate.instance and obstacle_term == candidate.terminal:
            continue
        if bbox_overlaps(candidate.landing_bbox_um, obstacle_bbox, include_touching=True):
            return False
    return True


def _via0_landing_margin_um(pdk: "PdkConfig") -> float:
    rules = pdk.rules
    values = []
    try:
        values.append(float(rules.min_width_um("M0")) / 2.0)
    except Exception:
        pass
    try:
        values.append(float(rules.enclosure("VIA0_M0")) * 1e-3)
    except Exception:
        pass
    try:
        values.append(float(rules.min_width_um("VIA0")) / 2.0)
    except Exception:
        pass
    return max(values or [0.03])


def _reference_sd_candidate_cost_adjustment(
    floorplan: "NativeStdCellFloorplan",
    *,
    net: str,
    xy: tuple[float, float],
) -> int:
    left_x, _, right_x, _ = floorplan.cell_bbox_um()
    center_x = (float(left_x) + float(right_x)) / 2.0
    half_span = max(float(right_x) - center_x, center_x - float(left_x), 1e-9)
    distance_from_center = abs(float(xy[0]) - center_x)
    normalized = min(max(distance_from_center / half_span, 0.0), 1.0)
    if str(net) in {"VDD", "VSS"}:
        # Prefer outboard rail-facing accesses for supply nets so the center
        # channel remains available to signal collectors.
        return int(round((1.0 - normalized) * 800.0))
    # Prefer interior signal access points, matching the compact reference
    # stdcell style where local signal routing stays inside the cell channel.
    return int(round(normalized * 1200.0))
