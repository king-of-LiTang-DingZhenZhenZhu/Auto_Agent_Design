"""SMT-facing problem builder and solver for native standard-cell placement."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from analogskills.contracts import LayoutConstraintSet, StandardCellConstraintSet, TopologyGraph
from analogskills.layout.stdcell_primitives import NativeStdCellTemplate, build_n7_native_stdcell_template

try:
    import z3  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    z3 = None


@dataclass(frozen=True)
class NativeStdCellPlacementProblem:
    graph_name: str
    template: NativeStdCellTemplate
    constraints: StandardCellConstraintSet
    device_order: tuple[str, ...]
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeStdCellPlacementSolution:
    template: NativeStdCellTemplate
    device_columns: tuple[tuple[str, int], ...]
    device_orientations: tuple[tuple[str, str], ...]
    pin_columns: tuple[tuple[str, int], ...]
    cost: float
    metadata: Mapping[str, object] = field(default_factory=dict)

    def device_column_map(self) -> dict[str, int]:
        return {name: int(column) for name, column in self.device_columns}

    def device_orientation_map(self) -> dict[str, str]:
        return {name: str(orient) for name, orient in self.device_orientations}

    def pin_column_map(self) -> dict[str, int]:
        return {name: int(column) for name, column in self.pin_columns}


def build_native_stdcell_placement_problem(
    graph: TopologyGraph,
    constraints: LayoutConstraintSet | None = None,
    *,
    template: NativeStdCellTemplate | None = None,
) -> NativeStdCellPlacementProblem:
    active = constraints or getattr(graph, "layout_constraints", None) or LayoutConstraintSet()
    std = active.standard_cell
    if std is None:
        raise ValueError("standard-cell constraints are required")
    resolved_template = template or build_n7_native_stdcell_template(max_device_columns=max(len(std.device_constraints), 4))
    ordered_devices = tuple(
        item.device
        for item in sorted(
            std.device_constraints,
            key=lambda item: (
                len(item.allowed_columns or resolved_template.placement_columns),
                len(item.order_before) + len(item.adjacent_to),
                item.device,
            ),
        )
    )
    terminal_nets = {
        str(device.name): {
            str(terminal.terminal): str(net.name)
            for net in graph.nets.values()
            for terminal in net.terminals
            if terminal.device == device.name
        }
        for device in graph.devices.values()
    }
    return NativeStdCellPlacementProblem(
        graph_name=graph.name,
        template=resolved_template,
        constraints=std,
        device_order=ordered_devices,
        metadata={"device_terminal_nets": terminal_nets},
    )


def solve_native_stdcell_placement(
    problem: NativeStdCellPlacementProblem,
    *,
    backend: str = "z3",
) -> NativeStdCellPlacementSolution:
    if backend != "z3":
        raise ValueError(f"unsupported native stdcell backend: {backend!r}")
    if z3 is None:
        raise RuntimeError("z3-solver is not installed")
    return _solve_native_stdcell_placement_z3(problem)


def _solve_native_stdcell_placement_z3(problem: NativeStdCellPlacementProblem) -> NativeStdCellPlacementSolution:
    solver = z3.Optimize()
    template = problem.template
    device_constraints = {item.device: item for item in problem.constraints.device_constraints}
    net_constraints = {item.net: item for item in problem.constraints.net_constraints}
    device_vars: dict[str, Any] = {}
    orient_vars: dict[str, Any] = {}
    pin_vars: dict[str, Any] = {}
    orient_maps: dict[str, dict[str, int]] = {}

    for name in problem.device_order:
        item = device_constraints[name]
        domain = tuple(item.allowed_columns or template.placement_columns)
        device_var = z3.Int(f"stdcell_col_{name}")
        solver.add(z3.Or([device_var == int(value) for value in domain]))
        device_vars[name] = device_var

        orientations = tuple(item.allowed_orientations or ((item.fixed_orientation,) if item.fixed_orientation else ("R0", "MY")))
        orient_map = {str(value): index for index, value in enumerate(dict.fromkeys(orientations))}
        orient_var = z3.Int(f"stdcell_orient_{name}")
        solver.add(z3.Or([orient_var == value for value in orient_map.values()]))
        if item.fixed_orientation is not None:
            solver.add(orient_var == orient_map[str(item.fixed_orientation)])
        orient_vars[name] = orient_var
        orient_maps[name] = orient_map

    for name, item in sorted(net_constraints.items()):
        if not item.allowed_pin_columns:
            continue
        pin_var = z3.Int(f"stdcell_pin_{name}")
        solver.add(z3.Or([pin_var == int(value) for value in item.allowed_pin_columns]))
        pin_vars[name] = pin_var

    rows = tuple(dict.fromkeys(item.row for item in problem.constraints.device_constraints))
    for row in rows:
        row_devices = [item.device for item in problem.constraints.device_constraints if item.row == row]
        for index, left in enumerate(row_devices):
            for right in row_devices[index + 1 :]:
                solver.add(device_vars[left] != device_vars[right])

    for item in problem.constraints.device_constraints:
        for other in item.order_before:
            if other in device_vars:
                solver.add(device_vars[item.device] < device_vars[other])
        for other in item.adjacent_to:
            if other in device_vars:
                solver.add(z3.Abs(device_vars[item.device] - device_vars[other]) == 1)

    for pin_group in problem.constraints.pin_groups:
        vars_ = [pin_vars[net] for net in pin_group.nets if net in pin_vars]
        if pin_group.ordered:
            for left, right in zip(vars_, vars_[1:]):
                solver.add(left < right)
        if pin_group.max_span > 0 and vars_:
            solver.add(_z3_span(tuple(vars_)) <= int(pin_group.max_span))

    for item in problem.constraints.net_constraints:
        if item.net not in pin_vars or item.pin_order_index is None:
            continue
        for other in problem.constraints.net_constraints:
            if other.net not in pin_vars or other.pin_order_index is None:
                continue
            if item.pin_order_index < other.pin_order_index:
                solver.add(pin_vars[item.net] < pin_vars[other.net])

    width_expr = _z3_span(tuple(device_vars.values()))
    diffusion_terms: list[Any] = []
    anchor_terms: list[Any] = []
    pin_terms: list[Any] = []
    terminal_nets = problem.metadata.get("device_terminal_nets", {})
    rail_nets = tuple(str(net) for net in problem.constraints.rail_nets)

    seen_adjacent_pairs: set[tuple[str, str]] = set()
    for item in problem.constraints.device_constraints:
        if item.boundary_anchor == "left":
            anchor_terms.append(device_vars[item.device])
        elif item.boundary_anchor == "right":
            anchor_terms.append((max(template.placement_columns) - device_vars[item.device]))
        for other in item.adjacent_to:
            if other not in device_vars:
                continue
            pair = tuple(sorted((item.device, other)))
            if pair in seen_adjacent_pairs:
                continue
            seen_adjacent_pairs.add(pair)
            penalty = _adjacent_diffusion_penalty_expr(
                item.device,
                other,
                terminal_nets,
                device_vars,
                orient_vars,
                orient_maps,
                rail_nets=rail_nets,
            )
            diffusion_terms.append(z3.If(z3.Abs(device_vars[item.device] - device_vars[other]) == 1, penalty, 2))

    for pin_group in problem.constraints.pin_groups:
        vars_ = [pin_vars[net] for net in pin_group.nets if net in pin_vars]
        if vars_:
            pin_terms.append(_z3_span(tuple(vars_)))

    total_cost = z3.Int("stdcell_native_total_cost")
    solver.add(total_cost == width_expr * 1000 + z3.Sum(diffusion_terms) * 200 + z3.Sum(anchor_terms) * 20 + z3.Sum(pin_terms) * 10)
    solver.minimize(total_cost)
    if solver.check() != z3.sat:
        raise RuntimeError(f"native stdcell placement is infeasible for {problem.graph_name}")
    model = solver.model()
    device_columns = tuple((name, model.eval(var).as_long()) for name, var in sorted(device_vars.items()))
    device_orientations = tuple(
        (
            name,
            next(orient for orient, index in orient_maps[name].items() if index == model.eval(var).as_long()),
        )
        for name, var in sorted(orient_vars.items())
    )
    pin_columns = tuple((name, model.eval(var).as_long()) for name, var in sorted(pin_vars.items()))
    return NativeStdCellPlacementSolution(
        template=template,
        device_columns=device_columns,
        device_orientations=device_orientations,
        pin_columns=pin_columns,
        cost=float(model.eval(total_cost).as_long()),
        metadata={"solver": "z3"},
    )


def _adjacent_diffusion_penalty_expr(
    name_a: str,
    name_b: str,
    terminal_nets: Any,
    device_vars: Mapping[str, Any],
    orient_vars: Mapping[str, Any],
    orient_maps: Mapping[str, Mapping[str, int]],
    *,
    rail_nets: Sequence[str],
) -> Any:
    left_a, right_a = _device_side_net_expr(name_a, terminal_nets, orient_vars, orient_maps)
    left_b, right_b = _device_side_net_expr(name_b, terminal_nets, orient_vars, orient_maps)
    a_is_left = device_vars[name_a] < device_vars[name_b]
    inner_left = z3.If(a_is_left, right_a, right_b)
    inner_right = z3.If(a_is_left, left_b, left_a)
    shared = inner_left == inner_right
    rail_shared = z3.Or([inner_left == z3.StringVal(str(net)) for net in rail_nets]) if rail_nets else z3.BoolVal(False)
    return z3.If(shared, z3.If(rail_shared, 1, 0), 2)


def _device_side_net_expr(
    device_name: str,
    terminal_nets: Any,
    orient_vars: Mapping[str, Any],
    orient_maps: Mapping[str, Mapping[str, int]],
) -> tuple[Any, Any]:
    source_net = str(terminal_nets.get(device_name, {}).get("S", ""))
    drain_net = str(terminal_nets.get(device_name, {}).get("D", ""))
    my_value = orient_maps[device_name].get("MY")
    if my_value is None:
        return (z3.StringVal(source_net), z3.StringVal(drain_net))
    left_net = z3.If(orient_vars[device_name] == my_value, z3.StringVal(drain_net), z3.StringVal(source_net))
    right_net = z3.If(orient_vars[device_name] == my_value, z3.StringVal(source_net), z3.StringVal(drain_net))
    return (left_net, right_net)


def _z3_span(values: Sequence[Any]) -> Any:
    if not values:
        return z3.IntVal(0)
    if len(values) == 1:
        return z3.IntVal(1)
    return _z3_max(tuple(values)) - _z3_min(tuple(values)) + 1


def _z3_min(values: Sequence[Any]) -> Any:
    result = values[0]
    for value in values[1:]:
        result = z3.If(value < result, value, result)
    return result


def _z3_max(values: Sequence[Any]) -> Any:
    result = values[0]
    for value in values[1:]:
        result = z3.If(value > result, value, result)
    return result
