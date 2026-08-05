"""Render and validate paper-schematic connectivity review artifacts."""

from __future__ import annotations

import argparse
import json
import math
import re
from html import escape
from pathlib import Path
from typing import Any


_INSTANCE_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*)\s+\(([^)]*)\)\s+(\S+)"
)
_SUPPORTED_KINDS = {
    "nmos",
    "pmos",
    "resistor",
    "capacitor",
    "pnp_diode",
    "current_source",
    "opamp",
}


def load_schematic_spec(path: str | Path) -> dict[str, Any]:
    """Load and structurally validate one connectivity review specification."""

    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    errors = validate_schematic_spec(spec)
    if errors:
        raise ValueError("Invalid schematic spec:\n- " + "\n- ".join(errors))
    return spec


def validate_schematic_spec(spec: dict[str, Any]) -> list[str]:
    """Return schema/layout errors without inspecting a generated netlist."""

    errors: list[str] = []
    for key in ("topology", "subckt", "ports", "instances", "diagram"):
        if key not in spec:
            errors.append(f"missing top-level field '{key}'")
    if errors:
        return errors

    names: set[str] = set()
    route_nets = {
        str(route.get("net"))
        for route in spec["diagram"].get("routes", [])
    }
    for instance in spec["instances"]:
        name = str(instance.get("name", ""))
        if not name:
            errors.append("instance name is empty")
        elif name in names:
            errors.append(f"duplicate instance '{name}'")
        names.add(name)

        kind = str(instance.get("kind", ""))
        if kind not in _SUPPORTED_KINDS:
            errors.append(f"instance '{name}' has unsupported kind '{kind}'")
        terminals = instance.get("terminals")
        if not isinstance(terminals, list) or not terminals:
            errors.append(f"instance '{name}' has no terminals")
            continue
        if "layout" not in instance:
            errors.append(f"instance '{name}' has no layout")
        hidden = set(instance.get("hidden_pins", []))
        for terminal in terminals:
            pin = str(terminal.get("pin", ""))
            net = str(terminal.get("net", ""))
            if not pin or not net:
                errors.append(f"instance '{name}' has an incomplete terminal")
            elif pin not in hidden and net not in route_nets:
                errors.append(
                    f"instance '{name}' pin '{pin}' uses unrouted net '{net}'"
                )

    for route in spec["diagram"].get("routes", []):
        paths = route.get("paths")
        if not isinstance(paths, list) or not paths:
            errors.append(f"net '{route.get('net')}' has no route paths")
            continue
        for path in paths:
            if not isinstance(path, list) or len(path) < 2:
                errors.append(
                    f"net '{route.get('net')}' contains a route shorter than 2 points"
                )
    return errors


def parse_subckt_connectivity(
    netlist: str,
    subckt_name: str,
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    """Extract ordered ports and instance nets from one Spectre subcircuit."""

    lines = netlist.splitlines()
    start = None
    ports: tuple[str, ...] = ()
    subckt_re = re.compile(
        rf"^subckt\s+{re.escape(subckt_name)}\s+\(([^)]*)\)\s*$",
        re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = subckt_re.match(line.strip())
        if match:
            start = index + 1
            ports = tuple(match.group(1).split())
            break
    if start is None:
        raise ValueError(f"subckt '{subckt_name}' not found")

    instances: dict[str, dict[str, Any]] = {}
    for line in lines[start:]:
        stripped = line.strip()
        if re.match(r"^ends(?:\s|$)", stripped, re.IGNORECASE):
            break
        if not stripped or stripped.startswith("//"):
            continue
        match = _INSTANCE_RE.match(stripped)
        if not match:
            continue
        name, raw_nets, model = match.groups()
        instances[name] = {
            "nets": tuple(raw_nets.split()),
            "model": model,
        }
    return ports, instances


def validate_netlist_connectivity(
    netlist: str,
    spec: dict[str, Any],
) -> list[str]:
    """Compare a generated topology netlist with its reviewed connectivity."""

    errors = validate_schematic_spec(spec)
    if errors:
        return errors
    try:
        ports, actual = parse_subckt_connectivity(netlist, spec["subckt"])
    except ValueError as exc:
        return [str(exc)]

    expected_ports = tuple(spec["ports"])
    if ports != expected_ports:
        errors.append(f"ports differ: expected {expected_ports}, got {ports}")

    expected = {instance["name"]: instance for instance in spec["instances"]}
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        errors.append(f"missing instances: {', '.join(missing)}")
    if extra:
        errors.append(f"unexpected instances: {', '.join(extra)}")

    for name in sorted(set(expected) & set(actual)):
        expected_nets = tuple(
            terminal["net"] for terminal in expected[name]["terminals"]
        )
        actual_nets = actual[name]["nets"]
        if actual_nets != expected_nets:
            errors.append(
                f"{name} nets differ: expected {expected_nets}, got {actual_nets}"
            )
        expected_model = expected[name].get("netlist_model")
        if expected_model and actual[name]["model"] != expected_model:
            errors.append(
                f"{name} model differs: expected {expected_model}, "
                f"got {actual[name]['model']}"
            )
    return errors


def render_schematic_svg(spec: dict[str, Any]) -> str:
    """Render a deterministic, paper-oriented SVG from a connectivity spec."""

    errors = validate_schematic_spec(spec)
    if errors:
        raise ValueError("Invalid schematic spec:\n- " + "\n- ".join(errors))

    diagram = spec["diagram"]
    width = float(diagram.get("width", 1400))
    height = float(diagram.get("height", 900))
    title = escape(str(diagram.get("title", spec["topology"])))
    subtitle = escape(str(diagram.get("subtitle", "")))
    elements: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" '
        f'height="{height:g}" viewBox="0 0 {width:g} {height:g}" '
        f'role="img" aria-labelledby="title description">',
        f"<title id=\"title\">{title}</title>",
        f'<desc id="description">{escape(str(diagram.get("description", "")))}</desc>',
        "<style>",
        "text{font-family:Arial,Helvetica,sans-serif;fill:#17212b}",
        ".title{font-size:28px;font-weight:700}.subtitle{font-size:15px;fill:#52606d}",
        ".wire{fill:none;stroke:#263238;stroke-width:2.2;stroke-linejoin:round}",
        ".connector{fill:none;stroke:#455a64;stroke-width:1.6;stroke-linejoin:round}",
        ".device{fill:#fff;stroke:#17212b;stroke-width:2}",
        ".adaptation{fill:#fff8e8;stroke:#b45309;stroke-width:2;stroke-dasharray:7 5}",
        ".mos-channel{stroke-width:4;stroke-linecap:butt}.mos-gate{stroke-width:2.4}",
        ".mos-pin{font-size:10px;font-weight:700;fill:#52606d}",
        ".device-label{font-size:15px;font-weight:700}.annotation{font-size:12px;fill:#52606d}",
        ".net-label{font-size:14px;font-weight:700;fill:#0f4c5c}",
        ".pin-label{font-size:11px;fill:#52606d}.junction{fill:#263238}",
        ".note{font-size:13px;fill:#3f4d5a}.legend{font-size:12px;fill:#52606d}",
        "</style>",
        f'<rect x="0" y="0" width="{width:g}" height="{height:g}" fill="#ffffff"/>',
        f'<text class="title" x="48" y="46">{title}</text>',
        f'<text class="subtitle" x="48" y="72">{subtitle}</text>',
    ]

    routes = diagram.get("routes", [])
    route_paths: dict[str, list[list[tuple[float, float]]]] = {}
    for route in routes:
        net = str(route["net"])
        paths = [
            [(float(point[0]), float(point[1])) for point in path]
            for path in route["paths"]
        ]
        route_paths[net] = paths
        for path in paths:
            points = " ".join(f"{x:g},{y:g}" for x, y in path)
            elements.append(f'<polyline class="wire" points="{points}"/>')
            for x, y in path:
                elements.append(
                    f'<circle class="junction" cx="{x:g}" cy="{y:g}" r="3"/>'
                )

    pin_points: dict[tuple[str, str], tuple[float, float]] = {}
    for instance in spec["instances"]:
        pin_points.update(_instance_pin_points(instance))

    for instance in spec["instances"]:
        hidden = set(instance.get("hidden_pins", []))
        for terminal in instance["terminals"]:
            pin = terminal["pin"]
            if pin in hidden:
                continue
            point = pin_points[(instance["name"], pin)]
            target = _nearest_route_point(point, route_paths[terminal["net"]])
            elements.append(_connector(point, target))

    for instance in spec["instances"]:
        elements.extend(_draw_instance(instance))

    for label in diagram.get("labels", []):
        elements.append(
            f'<text class="net-label" x="{float(label["x"]):g}" '
            f'y="{float(label["y"]):g}" '
            f'text-anchor="{escape(str(label.get("anchor", "start")))}">'
            f'{escape(str(label["text"]))}</text>'
        )

    legend_y = float(diagram.get("legend_y", height - 100))
    elements.extend([
        f'<line x1="48" y1="{legend_y:g}" x2="88" y2="{legend_y:g}" '
        'class="wire"/>',
        f'<text class="legend" x="98" y="{legend_y + 4:g}">paper-aligned device/path</text>',
        f'<rect x="300" y="{legend_y - 13:g}" width="40" height="22" '
        'class="adaptation"/>',
        f'<text class="legend" x="350" y="{legend_y + 4:g}">implementation adaptation</text>',
    ])
    note_y = legend_y + 32
    for index, note in enumerate(diagram.get("notes", []), start=1):
        elements.append(
            f'<text class="note" x="48" y="{note_y:g}">'
            f'{index}. {escape(str(note))}</text>'
        )
        note_y += 20

    elements.append("</svg>")
    return "\n".join(elements) + "\n"


def _instance_pin_points(
    instance: dict[str, Any],
) -> dict[tuple[str, str], tuple[float, float]]:
    name = str(instance["name"])
    kind = str(instance["kind"])
    layout = instance["layout"]
    x = float(layout["x"])
    y = float(layout["y"])
    gate_side = -1.0 if layout.get("gate_side", "left") == "left" else 1.0
    flip = bool(layout.get("flip_vertical", False))

    if kind == "nmos":
        offsets = {"d": (0, -45), "s": (0, 45), "g": (38 * gate_side, 0), "b": (-38 * gate_side, 20)}
    elif kind == "pmos":
        offsets = {"s": (0, -45), "d": (0, 45), "g": (38 * gate_side, 0), "b": (-38 * gate_side, -20)}
    elif kind in {"resistor", "capacitor", "current_source"}:
        offsets = {"p": (0, -45), "n": (0, 45)}
    elif kind == "pnp_diode":
        offsets = {"e": (0, -45), "c": (0, 45), "b": (0, 45)}
    elif kind == "opamp":
        offsets = {
            "vip": (-62, 18),
            "vin": (-62, -18),
            "vout": (62, 0),
            "vdd": (0, -48),
            "vss": (0, 48),
            "ibias": (30, 48),
        }
    else:
        raise ValueError(f"Unsupported instance kind '{kind}'")

    if flip:
        offsets = {pin: (dx, -dy) for pin, (dx, dy) in offsets.items()}
    custom = layout.get("pin_offsets", {})
    for pin, point in custom.items():
        offsets[pin] = (float(point[0]), float(point[1]))
    return {
        (name, pin): (x + dx, y + dy)
        for pin, (dx, dy) in offsets.items()
    }


def _nearest_route_point(
    point: tuple[float, float],
    paths: list[list[tuple[float, float]]],
) -> tuple[float, float]:
    best = None
    best_distance = math.inf
    for path in paths:
        for start, end in zip(path, path[1:]):
            candidate = _nearest_segment_point(point, start, end)
            distance = math.dist(point, candidate)
            if distance < best_distance:
                best = candidate
                best_distance = distance
    if best is None:
        raise ValueError("route has no segments")
    return best


def _nearest_segment_point(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[float, float]:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return start
    fraction = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return x1 + fraction * dx, y1 + fraction * dy


def _connector(
    start: tuple[float, float],
    end: tuple[float, float],
) -> str:
    x1, y1 = start
    x2, y2 = end
    if math.isclose(x1, x2) or math.isclose(y1, y2):
        points = f"{x1:g},{y1:g} {x2:g},{y2:g}"
    else:
        points = f"{x1:g},{y1:g} {x2:g},{y1:g} {x2:g},{y2:g}"
    return f'<polyline class="connector" points="{points}"/>'


def _draw_instance(instance: dict[str, Any]) -> list[str]:
    kind = str(instance["kind"])
    layout = instance["layout"]
    x = float(layout["x"])
    y = float(layout["y"])
    gate_side = -1.0 if layout.get("gate_side", "left") == "left" else 1.0
    adaptation = instance.get("alignment") == "implementation_adaptation"
    device_class = "adaptation" if adaptation else "device"
    items: list[str] = []

    if kind in {"nmos", "pmos"}:
        top_pin = "S" if kind == "pmos" else "D"
        bottom_pin = "D" if kind == "pmos" else "S"
        text_side = -gate_side
        pin_anchor = "start" if text_side > 0 else "end"
        gate_anchor = "start" if gate_side > 0 else "end"
        items.extend([
            f'<line class="{device_class}" x1="{x:g}" y1="{y - 45:g}" x2="{x:g}" y2="{y - 22:g}"/>',
            f'<line class="{device_class} mos-channel" x1="{x:g}" y1="{y - 22:g}" x2="{x:g}" y2="{y + 22:g}"/>',
            f'<line class="{device_class}" x1="{x:g}" y1="{y + 22:g}" x2="{x:g}" y2="{y + 45:g}"/>',
            f'<line class="{device_class} mos-gate" x1="{x + 18 * gate_side:g}" y1="{y - 25:g}" x2="{x + 18 * gate_side:g}" y2="{y + 25:g}"/>',
            f'<text class="mos-pin" x="{x + 12 * text_side:g}" y="{y - 27:g}" text-anchor="{pin_anchor}">{top_pin}</text>',
            f'<text class="mos-pin" x="{x + 12 * text_side:g}" y="{y + 36:g}" text-anchor="{pin_anchor}">{bottom_pin}</text>',
            f'<text class="mos-pin" x="{x + 44 * gate_side:g}" y="{y - 5:g}" text-anchor="{gate_anchor}">G</text>',
        ])
        if kind == "pmos":
            items.extend([
                f'<line class="{device_class}" x1="{x + 38 * gate_side:g}" y1="{y:g}" x2="{x + 29 * gate_side:g}" y2="{y:g}"/>',
                f'<circle cx="{x + 24 * gate_side:g}" cy="{y:g}" r="5" class="{device_class}"/>',
                f'<line class="{device_class}" x1="{x + 19 * gate_side:g}" y1="{y:g}" x2="{x + 18 * gate_side:g}" y2="{y:g}"/>',
            ])
        else:
            items.append(
                f'<line class="{device_class}" x1="{x + 38 * gate_side:g}" y1="{y:g}" x2="{x + 18 * gate_side:g}" y2="{y:g}"/>'
            )
    elif kind == "resistor":
        points = [
            (x, y - 45), (x, y - 30), (x - 10, y - 22), (x + 10, y - 12),
            (x - 10, y - 2), (x + 10, y + 8), (x - 10, y + 18),
            (x, y + 28), (x, y + 45),
        ]
        items.append(
            f'<polyline class="{device_class}" points="'
            + " ".join(f"{px:g},{py:g}" for px, py in points)
            + '"/>'
        )
    elif kind == "capacitor":
        items.extend([
            f'<line class="{device_class}" x1="{x:g}" y1="{y - 45:g}" x2="{x:g}" y2="{y - 7:g}"/>',
            f'<line class="{device_class}" x1="{x - 18:g}" y1="{y - 7:g}" x2="{x + 18:g}" y2="{y - 7:g}"/>',
            f'<line class="{device_class}" x1="{x - 18:g}" y1="{y + 7:g}" x2="{x + 18:g}" y2="{y + 7:g}"/>',
            f'<line class="{device_class}" x1="{x:g}" y1="{y + 7:g}" x2="{x:g}" y2="{y + 45:g}"/>',
        ])
    elif kind == "pnp_diode":
        items.extend([
            f'<line class="{device_class}" x1="{x:g}" y1="{y - 45:g}" x2="{x:g}" y2="{y - 25:g}"/>',
            f'<circle class="{device_class}" cx="{x:g}" cy="{y:g}" r="25"/>',
            f'<path class="{device_class}" d="M {x - 11:g} {y - 9:g} L {x + 11:g} {y:g} L {x - 11:g} {y + 9:g} Z"/>',
            f'<line class="{device_class}" x1="{x - 12:g}" y1="{y + 12:g}" x2="{x + 12:g}" y2="{y + 12:g}"/>',
            f'<line class="{device_class}" x1="{x:g}" y1="{y + 25:g}" x2="{x:g}" y2="{y + 45:g}"/>',
        ])
    elif kind == "current_source":
        items.extend([
            f'<line class="{device_class}" x1="{x:g}" y1="{y - 45:g}" x2="{x:g}" y2="{y - 24:g}"/>',
            f'<circle class="{device_class}" cx="{x:g}" cy="{y:g}" r="24"/>',
            f'<line class="{device_class}" x1="{x:g}" y1="{y + 24:g}" x2="{x:g}" y2="{y + 45:g}"/>',
            f'<path class="{device_class}" d="M {x:g} {y + 13:g} L {x:g} {y - 13:g} M {x - 6:g} {y - 5:g} L {x:g} {y - 13:g} L {x + 6:g} {y - 5:g}"/>',
        ])
    elif kind == "opamp":
        items.extend([
            f'<path class="{device_class}" d="M {x - 50:g} {y - 40:g} L {x - 50:g} {y + 40:g} L {x + 55:g} {y:g} Z"/>',
            f'<text class="pin-label" x="{x - 43:g}" y="{y - 14:g}">-</text>',
            f'<text class="pin-label" x="{x - 43:g}" y="{y + 23:g}">+</text>',
        ])
    else:
        raise ValueError(f"Unsupported instance kind '{kind}'")

    label_x = x + float(layout.get("label_dx", 48))
    label_y = y + float(layout.get("label_dy", -28))
    items.append(
        f'<text class="device-label" x="{label_x:g}" y="{label_y:g}">'
        f'{escape(str(instance["name"]))}</text>'
    )
    annotation = instance.get("annotation")
    if annotation:
        items.append(
            f'<text class="annotation" x="{label_x:g}" y="{label_y + 17:g}">'
            f'{escape(str(annotation))}</text>'
        )
    hidden = set(instance.get("hidden_pins", []))
    hidden_values = [
        f"{terminal['pin'].upper()}={terminal['net']}"
        for terminal in instance["terminals"]
        if terminal["pin"] in hidden
    ]
    if hidden_values:
        items.append(
            f'<text class="pin-label" x="{label_x:g}" y="{label_y + 34:g}">'
            f'{escape(", ".join(hidden_values))}</text>'
        )
    return items


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a reviewed connectivity JSON as SVG"
    )
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--topology",
        help="Also validate the registered topology netlist against the spec",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    spec = load_schematic_spec(args.spec)
    if args.topology:
        from topologies import get_topology

        netlist = get_topology(args.topology).generate_circuit()
        errors = validate_netlist_connectivity(netlist, spec)
        if errors:
            raise SystemExit("Connectivity validation failed:\n- " + "\n- ".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_schematic_svg(spec), encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
