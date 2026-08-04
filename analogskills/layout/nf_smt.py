"""SMT selection of PCell finger realizations for fixed electrical sizing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None


@dataclass(frozen=True)
class FingerRealization:
    nf: int
    m: int
    finger_width_nm: int
    width_sites: int
    height_sites: int
    access_cost: int = 0
    drc_clean: bool = True
    bbox_x0_sites: int = 0
    bbox_y0_sites: int = 0
    electrical_total_width_nm: int = 0
    intrinsic_drc_cost: int = 0
    pcell_params: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FixedSizeDevice:
    name: str
    total_width_nm: int
    length_nm: int
    candidates: tuple[FingerRealization, ...]
    row: int = 0


@dataclass(frozen=True)
class MatchedPair:
    left: str
    right: str


@dataclass(frozen=True)
class FingerPlacement:
    name: str
    nf: int
    m: int
    finger_width_nm: int
    x_sites: int
    y_sites: int
    width_sites: int
    height_sites: int
    orient: str
    pcell_origin_x_sites: int
    pcell_origin_y_sites: int
    pcell_bbox_x0_sites: int
    pcell_bbox_y0_sites: int
    pcell_params: Mapping[str, object]


@dataclass(frozen=True)
class FingerSmtSolution:
    placements: Mapping[str, FingerPlacement]
    total_width_sites: int
    total_height_sites: int


def solve_fixed_size_finger_placement(
    devices: tuple[FixedSizeDevice, ...],
    *,
    matched_pairs: tuple[MatchedPair, ...] = (),
    spacing_sites: int = 1,
    row_spacing_sites: int = 1,
    max_matched_pair_gap_sites: int | None = None,
    max_row_spacing_sites: int | None = None,
    row_spacing_overrides_sites: Mapping[tuple[int, int], int] | None = None,
) -> FingerSmtSolution:
    """Choose electrically equivalent nf candidates and compact row placement.

    Candidate geometry is expected to come from PCell characterization. Invalid
    candidates are rejected before the SMT problem is built.
    """
    if z3 is None:  # pragma: no cover
        raise RuntimeError("z3-solver is required for finger placement")
    if spacing_sites < 0 or row_spacing_sites < 0 or not devices:
        raise ValueError("invalid finger-placement problem")
    pair_gap_max = spacing_sites if max_matched_pair_gap_sites is None else max_matched_pair_gap_sites
    row_gap_max = row_spacing_sites if max_row_spacing_sites is None else max_row_spacing_sites
    if pair_gap_max < spacing_sites or row_gap_max < row_spacing_sites:
        raise ValueError("maximum proximity gaps cannot be smaller than minimum spacing")
    row_spacing_overrides = {
        (int(lower), int(upper)): int(value)
        for (lower, upper), value in dict(row_spacing_overrides_sites or {}).items()
    }
    for key, value in row_spacing_overrides.items():
        if value < row_spacing_sites:
            raise ValueError(f"row spacing override {key} cannot be smaller than default row spacing")
    by_name = {device.name: device for device in devices}
    if len(by_name) != len(devices):
        raise ValueError("device names must be unique")
    legal: dict[str, tuple[FingerRealization, ...]] = {}
    for device in devices:
        rows = tuple(c for c in device.candidates if c.drc_clean and c.nf > 0 and c.m > 0 and c.width_sites > 0 and c.height_sites > 0)
        rows = tuple(c for c in rows if (c.electrical_total_width_nm or c.nf * c.m * c.finger_width_nm) == device.total_width_nm)
        if not rows:
            raise ValueError(f"device {device.name} has no DRC-clean, fixed-width finger realization")
        legal[device.name] = rows

    opt = z3.Optimize()
    choice = {d.name: z3.Int(f"nf_choice__{d.name}") for d in devices}
    x = {d.name: z3.Int(f"nf_x__{d.name}") for d in devices}
    y = {d.name: z3.Int(f"nf_y__{d.name}") for d in devices}
    width = {d.name: _select(choice[d.name], tuple(c.width_sites for c in legal[d.name])) for d in devices}
    height = {d.name: _select(choice[d.name], tuple(c.height_sites for c in legal[d.name])) for d in devices}
    for d in devices:
        opt.add(choice[d.name] >= 0, choice[d.name] < len(legal[d.name]), x[d.name] >= 0, y[d.name] >= 0)

    rows: dict[int, list[FixedSizeDevice]] = {}
    for d in devices:
        rows.setdefault(d.row, []).append(d)
    row_ids = sorted(rows)
    row_y = {row: z3.Int(f"nf_row_y__{row}") for row in row_ids}
    row_h = {row: z3.Int(f"nf_row_h__{row}") for row in row_ids}
    for row in row_ids:
        opt.add(row_y[row] >= 0)
        for d in rows[row]:
            opt.add(y[d.name] == row_y[row], row_h[row] >= height[d.name])
    for lower, upper in zip(row_ids, row_ids[1:]):
        min_gap = row_spacing_overrides.get((lower, upper), row_spacing_sites)
        max_gap = max(row_gap_max, min_gap)
        opt.add(row_y[upper] >= row_y[lower] + row_h[lower] + min_gap)
        opt.add(row_y[upper] <= row_y[lower] + row_h[lower] + max_gap)

    paired_names: set[str] = set()
    axis2 = z3.Int("nf_symmetry_axis2")
    opt.add(axis2 >= 0)
    for pair in matched_pairs:
        if pair.left not in by_name or pair.right not in by_name:
            raise ValueError("matched pair references an unknown device")
        if by_name[pair.left].row != by_name[pair.right].row:
            raise ValueError("matched pair devices must occupy the same row")
        left_rows, right_rows = legal[pair.left], legal[pair.right]
        if len(left_rows) != len(right_rows) or any((a.nf, a.m, a.finger_width_nm, a.width_sites, a.height_sites) != (b.nf, b.m, b.finger_width_nm, b.width_sites, b.height_sites) for a, b in zip(left_rows, right_rows)):
            raise ValueError("matched pair candidate tables must be identical")
        opt.add(choice[pair.left] == choice[pair.right])
        opt.add(2 * x[pair.left] + width[pair.left] + 2 * x[pair.right] + width[pair.right] == 2 * axis2)
        opt.add(x[pair.left] + width[pair.left] + spacing_sites <= x[pair.right])
        opt.add(x[pair.right] <= x[pair.left] + width[pair.left] + pair_gap_max)
        paired_names.update((pair.left, pair.right))

    for row_devices in rows.values():
        for i, left in enumerate(row_devices):
            for right in row_devices[i + 1:]:
                if left.name in paired_names and right.name in paired_names:
                    continue
                opt.add(z3.Or(x[left.name] + width[left.name] + spacing_sites <= x[right.name], x[right.name] + width[right.name] + spacing_sites <= x[left.name]))

    total_w, total_h = z3.Int("nf_total_width"), z3.Int("nf_total_height")
    for d in devices:
        opt.add(total_w >= x[d.name] + width[d.name], total_h >= y[d.name] + height[d.name])
    drc_cost = z3.Sum([_select(choice[d.name], tuple(c.intrinsic_drc_cost for c in legal[d.name])) for d in devices])
    candidate_cost = z3.Sum([_select(choice[d.name], tuple(c.access_cost + c.width_sites * c.height_sites for c in legal[d.name])) for d in devices])
    unpaired_axis_terms = [
        _abs_int(2 * x[d.name] + width[d.name] - axis2)
        for d in devices
        if matched_pairs and d.name not in paired_names
    ]
    opt.minimize(drc_cost)
    opt.minimize(total_w + total_h)
    if unpaired_axis_terms:
        opt.minimize(z3.Sum(unpaired_axis_terms))
    opt.minimize(candidate_cost)
    if opt.check() != z3.sat:
        raise ValueError("fixed-size finger placement is unsatisfiable")
    model = opt.model()
    placements: dict[str, FingerPlacement] = {}
    right_names = {pair.right for pair in matched_pairs}
    for d in devices:
        idx = model.eval(choice[d.name]).as_long()
        c = legal[d.name][idx]
        px, py = model.eval(x[d.name]).as_long(), model.eval(y[d.name]).as_long()
        mirrored = d.name in right_names
        origin_x = px + c.bbox_x0_sites + c.width_sites if mirrored else px - c.bbox_x0_sites
        placements[d.name] = FingerPlacement(d.name, c.nf, c.m, c.finger_width_nm, px, py, c.width_sites, c.height_sites, "MY" if mirrored else "R0", origin_x, py - c.bbox_y0_sites, c.bbox_x0_sites, c.bbox_y0_sites, dict(c.pcell_params))
    return FingerSmtSolution(placements, model.eval(total_w).as_long(), model.eval(total_h).as_long())


def _select(index: object, values: tuple[int, ...]) -> object:
    expr = z3.IntVal(values[-1])
    for idx in range(len(values) - 2, -1, -1):
        expr = z3.If(index == idx, values[idx], expr)
    return expr


def _abs_int(value: object) -> object:
    return z3.If(value >= 0, value, -value)
