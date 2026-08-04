"""Early placement routability estimation using bin-based net demand."""
from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Mapping

from analogskills.contracts import TopologyGraph
from .placement import Placement


@dataclass(frozen=True)
class PlacementRoutabilityReport:
    demand_by_bin: Mapping[tuple[int, int], float]
    overflow_by_bin: Mapping[tuple[int, int], float]
    hotspot_count: int
    total_overflow: float
    peak_utilization: float
    estimated_wirelength_um: float

    @property
    def passed(self) -> bool:
        return self.total_overflow <= 1e-12


def analyze_placement_routability(
    placements: tuple[Placement, ...],
    graph: TopologyGraph,
    *,
    bin_size_um: float | None = None,
    bin_capacity: float = 4.0,
) -> PlacementRoutabilityReport:
    if bin_capacity <= 0.0:
        raise ValueError("bin_capacity must be positive")
    centers = _device_centers(placements)
    if not centers:
        return PlacementRoutabilityReport({}, {}, 0, 0.0, 0.0, 0.0)
    size = float(bin_size_um) if bin_size_um is not None else _default_bin_size(centers)
    if size <= 0.0:
        raise ValueError("bin_size_um must be positive")
    demand: dict[tuple[int, int], float] = {}
    wirelength = 0.0
    critical = set(graph.layout_constraints.critical_nets)
    for net in graph.nets.values():
        points = tuple(centers[terminal.device] for terminal in net.terminals if terminal.device in centers)
        if len(points) < 2:
            continue
        x0, x1 = min(point[0] for point in points), max(point[0] for point in points)
        y0, y1 = min(point[1] for point in points), max(point[1] for point in points)
        wirelength += (x1 - x0) + (y1 - y0)
        weight = 2.0 if net.name in critical else 1.0
        x_bins = range(floor(x0 / size), floor(x1 / size) + 1)
        y_bins = range(floor(y0 / size), floor(y1 / size) + 1)
        bins = tuple((x_bin, y_bin) for x_bin in x_bins for y_bin in y_bins)
        per_bin = weight / max(len(bins), 1)
        for bin_id in bins:
            demand[bin_id] = demand.get(bin_id, 0.0) + per_bin
    overflow = {bin_id: value - bin_capacity for bin_id, value in demand.items() if value > bin_capacity}
    peak = max((value / bin_capacity for value in demand.values()), default=0.0)
    return PlacementRoutabilityReport(
        demand_by_bin=dict(sorted(demand.items())),
        overflow_by_bin=dict(sorted(overflow.items())),
        hotspot_count=len(overflow),
        total_overflow=sum(overflow.values()),
        peak_utilization=peak,
        estimated_wirelength_um=wirelength,
    )


def _device_centers(placements: tuple[Placement, ...]) -> dict[str, tuple[float, float]]:
    grouped: dict[str, list[Placement]] = {}
    for placement in placements:
        role = placement.role or placement.name
        if role == "dummy":
            continue
        grouped.setdefault(role, []).append(placement)
    return {
        role: (
            sum(item.x_um for item in items) / len(items),
            sum(item.y_um for item in items) / len(items),
        )
        for role, items in grouped.items()
    }


def _default_bin_size(centers: Mapping[str, tuple[float, float]]) -> float:
    xs = [point[0] for point in centers.values()]
    ys = [point[1] for point in centers.values()]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    return max(span / max(round(len(centers) ** 0.5), 1), 1e-6)
